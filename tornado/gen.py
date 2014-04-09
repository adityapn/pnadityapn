"""``tornado.gen`` is a generator-based interface to make it easier to
work in an asynchronous environment.  Code using the ``gen`` module
is technically asynchronous, but it is written as a single generator
instead of a collection of separate functions.

For example, the following asynchronous handler::

    class AsyncHandler(RequestHandler):
        @asynchronous
        def get(self):
            http_client = AsyncHTTPClient()
            http_client.fetch("http://example.com",
                              callback=self.on_fetch)

        def on_fetch(self, response):
            do_something_with_response(response)
            self.render("template.html")

could be written with ``gen`` as::

    class GenAsyncHandler(RequestHandler):
        @gen.coroutine
        def get(self):
            http_client = AsyncHTTPClient()
            response = yield http_client.fetch("http://example.com")
            do_something_with_response(response)
            self.render("template.html")

Most asynchronous functions in Tornado return a `.Future`;
yielding this object returns its `~.Future.result`.

For functions that do not return ``Futures``, `Task` works with any
function that takes a ``callback`` keyword argument (most Tornado functions
can be used in either style, although the ``Future`` style is preferred
since it is both shorter and provides better exception handling)::

    @gen.coroutine
    def get(self):
        yield gen.Task(AsyncHTTPClient().fetch, "http://example.com")

You can also yield a list or dict of ``Futures`` and/or ``Tasks``, which will be
started at the same time and run in parallel; a list or dict of results will
be returned when they are all finished::

    @gen.coroutine
    def get(self):
        http_client = AsyncHTTPClient()
        response1, response2 = yield [http_client.fetch(url1),
                                      http_client.fetch(url2)]
        response_dict = yield dict(response3=http_client.fetch(url3),
                                   response4=http_client.fetch(url4))
        response3 = response_dict['response3']
        response4 = response_dict['response4']

.. versionchanged:: 3.2
   Dict support added.

For more complicated interfaces, `Task` can be split into two parts:
`Callback` and `Wait`::

    class GenAsyncHandler2(RequestHandler):
        @gen.coroutine
        def get(self):
            http_client = AsyncHTTPClient()
            http_client.fetch("http://example.com",
                              callback=(yield gen.Callback("key")))
            response = yield gen.Wait("key")
            do_something_with_response(response)
            self.render("template.html")

The ``key`` argument to `Callback` and `Wait` allows for multiple
asynchronous operations to be started at different times and proceed
in parallel: yield several callbacks with different keys, then wait
for them once all the async operations have started.

The result of a `Wait` or `Task` yield expression depends on how the callback
was run.  If it was called with no arguments, the result is ``None``.  If
it was called with one argument, the result is that argument.  If it was
called with more than one argument or any keyword arguments, the result
is an `Arguments` object, which is a named tuple ``(args, kwargs)``.
"""
from __future__ import absolute_import, division, print_function, with_statement

import collections
import functools
import itertools
import sys
import types

from tornado.concurrent import Future, TracebackFuture, is_future
from tornado.ioloop import IOLoop
from tornado import stack_context


class KeyReuseError(Exception):
    pass


class UnknownKeyError(Exception):
    pass


class LeakedCallbackError(Exception):
    pass


class BadYieldError(Exception):
    pass


class ReturnValueIgnoredError(Exception):
    pass


def engine(func):
    """Callback-oriented decorator for asynchronous generators.

    This is an older interface; for new code that does not need to be
    compatible with versions of Tornado older than 3.0 the
    `coroutine` decorator is recommended instead.

    This decorator is similar to `coroutine`, except it does not
    return a `.Future` and the ``callback`` argument is not treated
    specially.

    In most cases, functions decorated with `engine` should take
    a ``callback`` argument and invoke it with their result when
    they are finished.  One notable exception is the
    `~tornado.web.RequestHandler` :ref:`HTTP verb methods <verbs>`,
    which use ``self.finish()`` in place of a callback argument.
    """
    func = _make_coroutine_wrapper(func, replace_callback=False)
    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        future = func(*args, **kwargs)
        def final_callback(future):
            if future.result() is not None:
                raise ReturnValueIgnoredError(
                    "@gen.engine functions cannot return values: %r" %
                    (future.result(),))
        future.add_done_callback(final_callback)
    return wrapper


def coroutine(func, replace_callback=True):
    """Decorator for asynchronous generators.

    Any generator that yields objects from this module must be wrapped
    in either this decorator or `engine`.

    Coroutines may "return" by raising the special exception
    `Return(value) <Return>`.  In Python 3.3+, it is also possible for
    the function to simply use the ``return value`` statement (prior to
    Python 3.3 generators were not allowed to also return values).
    In all versions of Python a coroutine that simply wishes to exit
    early may use the ``return`` statement without a value.

    Functions with this decorator return a `.Future`.  Additionally,
    they may be called with a ``callback`` keyword argument, which
    will be invoked with the future's result when it resolves.  If the
    coroutine fails, the callback will not be run and an exception
    will be raised into the surrounding `.StackContext`.  The
    ``callback`` argument is not visible inside the decorated
    function; it is handled by the decorator itself.

    From the caller's perspective, ``@gen.coroutine`` is similar to
    the combination of ``@return_future`` and ``@gen.engine``.
    """
    return _make_coroutine_wrapper(func, replace_callback=True)


def _make_coroutine_wrapper(func, replace_callback):
    """The inner workings of ``@gen.coroutine`` and ``@gen.engine``.

    The two decorators differ in their treatment of the ``callback``
    argument, so we cannot simply implement ``@engine`` in terms of
    ``@coroutine``.
    """
    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        runner = None
        future = TracebackFuture()

        if replace_callback and 'callback' in kwargs:
            callback = kwargs.pop('callback')
            IOLoop.current().add_future(
                future, lambda future: callback(future.result()))

        try:
            result = func(*args, **kwargs)
        except (Return, StopIteration) as e:
            result = getattr(e, 'value', None)
        except Exception:
            future.set_exc_info(sys.exc_info())
            return future
        else:
            if isinstance(result, types.GeneratorType):
                runner = Runner(result, future)
                runner.run()
                return future
        future.set_result(result)
        return future
    return wrapper


class Return(Exception):
    """Special exception to return a value from a `coroutine`.

    If this exception is raised, its value argument is used as the
    result of the coroutine::

        @gen.coroutine
        def fetch_json(url):
            response = yield AsyncHTTPClient().fetch(url)
            raise gen.Return(json_decode(response.body))

    In Python 3.3, this exception is no longer necessary: the ``return``
    statement can be used directly to return a value (previously
    ``yield`` and ``return`` with a value could not be combined in the
    same function).

    By analogy with the return statement, the value argument is optional,
    but it is never necessary to ``raise gen.Return()``.  The ``return``
    statement can be used with no arguments instead.
    """
    def __init__(self, value=None):
        super(Return, self).__init__()
        self.value = value


class YieldPoint(object):
    """Base class for objects that may be yielded from the generator.

    Applications do not normally need to use this class, but it may be
    subclassed to provide additional yielding behavior.
    """
    def start(self, runner):
        """Called by the runner after the generator has yielded.

        No other methods will be called on this object before ``start``.
        """
        raise NotImplementedError()

    def is_ready(self):
        """Called by the runner to determine whether to resume the generator.

        Returns a boolean; may be called more than once.
        """
        raise NotImplementedError()

    def get_result(self):
        """Returns the value to use as the result of the yield expression.

        This method will only be called once, and only after `is_ready`
        has returned true.
        """
        raise NotImplementedError()


class Callback(YieldPoint):
    """Returns a callable object that will allow a matching `Wait` to proceed.

    The key may be any value suitable for use as a dictionary key, and is
    used to match ``Callbacks`` to their corresponding ``Waits``.  The key
    must be unique among outstanding callbacks within a single run of the
    generator function, but may be reused across different runs of the same
    function (so constants generally work fine).

    The callback may be called with zero or one arguments; if an argument
    is given it will be returned by `Wait`.
    """
    def __init__(self, key):
        self.key = key

    def start(self, runner):
        self.runner = runner
        runner.register_callback(self.key)

    def is_ready(self):
        return True

    def get_result(self):
        return self.runner.result_callback(self.key)


class Wait(YieldPoint):
    """Returns the argument passed to the result of a previous `Callback`."""
    def __init__(self, key):
        self.key = key

    def start(self, runner):
        self.runner = runner

    def is_ready(self):
        return self.runner.is_ready(self.key)

    def get_result(self):
        return self.runner.pop_result(self.key)


class WaitAll(YieldPoint):
    """Returns the results of multiple previous `Callbacks <Callback>`.

    The argument is a sequence of `Callback` keys, and the result is
    a list of results in the same order.

    `WaitAll` is equivalent to yielding a list of `Wait` objects.
    """
    def __init__(self, keys):
        self.keys = keys

    def start(self, runner):
        self.runner = runner

    def is_ready(self):
        return all(self.runner.is_ready(key) for key in self.keys)

    def get_result(self):
        return [self.runner.pop_result(key) for key in self.keys]


class Task(YieldPoint):
    """Runs a single asynchronous operation.

    Takes a function (and optional additional arguments) and runs it with
    those arguments plus a ``callback`` keyword argument.  The argument passed
    to the callback is returned as the result of the yield expression.

    A `Task` is equivalent to a `Callback`/`Wait` pair (with a unique
    key generated automatically)::

        result = yield gen.Task(func, args)

        func(args, callback=(yield gen.Callback(key)))
        result = yield gen.Wait(key)
    """
    def __init__(self, func, *args, **kwargs):
        assert "callback" not in kwargs
        self.args = args
        self.kwargs = kwargs
        self.func = func

    def start(self, runner):
        self.runner = runner
        self.key = object()
        runner.register_callback(self.key)
        self.kwargs["callback"] = runner.result_callback(self.key)
        self.func(*self.args, **self.kwargs)

    def is_ready(self):
        return self.runner.is_ready(self.key)

    def get_result(self):
        return self.runner.pop_result(self.key)


class YieldFuture(YieldPoint):
    def __init__(self, future, io_loop=None):
        self.future = future
        self.io_loop = io_loop or IOLoop.current()

    def start(self, runner):
        if not self.future.done():
            self.runner = runner
            self.key = object()
            runner.register_callback(self.key)
            self.io_loop.add_future(self.future, runner.result_callback(self.key))
        else:
            self.runner = None
            self.result = self.future.result()

    def is_ready(self):
        if self.runner is not None:
            return self.runner.is_ready(self.key)
        else:
            return True

    def get_result(self):
        if self.runner is not None:
            return self.runner.pop_result(self.key).result()
        else:
            return self.result


class Multi(YieldPoint):
    """Runs multiple asynchronous operations in parallel.

    Takes a list of ``Tasks`` or other ``YieldPoints`` and returns a list of
    their responses.  It is not necessary to call `Multi` explicitly,
    since the engine will do so automatically when the generator yields
    a list of ``YieldPoints``.
    """
    def __init__(self, children):
        self.keys = None
        if isinstance(children, dict):
            self.keys = list(children.keys())
            children = children.values()
        self.children = []
        for i in children:
            if is_future(i):
                i = YieldFuture(i)
            self.children.append(i)
        assert all(isinstance(i, YieldPoint) for i in self.children)
        self.unfinished_children = set(self.children)

    def start(self, runner):
        for i in self.children:
            i.start(runner)

    def is_ready(self):
        finished = list(itertools.takewhile(
            lambda i: i.is_ready(), self.unfinished_children))
        self.unfinished_children.difference_update(finished)
        return not self.unfinished_children

    def get_result(self):
        result = (i.get_result() for i in self.children)
        if self.keys is not None:
            return dict(zip(self.keys, result))
        else:
            return list(result)


_null_future = Future()
_null_future.set_result(None)


class Runner(object):
    """Internal implementation of `tornado.gen.engine`.

    Maintains information about pending callbacks and their results.

    The results of the generator are stored in ``result_future`` (a
    `.TracebackFuture`)
    """
    def __init__(self, gen, result_future):
        self.gen = gen
        self.result_future = result_future
        self.future = _null_future
        self.yield_point = None
        self.pending_callbacks = set()
        self.results = {}
        self.running = False
        self.finished = False
        self.exc_info = None
        self.had_exception = False
        self.io_loop = IOLoop.current()
        # For efficiency, we do not create a stack context until we
        # reach a YieldPoint (stack contexts are required for the historical
        # semantics of YieldPoints, but not for Futures).  When we have
        # done so, this field will be set and must be called at the end
        # of the coroutine.
        self.stack_context_deactivate = None

    def register_callback(self, key):
        """Adds ``key`` to the list of callbacks."""
        if key in self.pending_callbacks:
            raise KeyReuseError("key %r is already pending" % (key,))
        self.pending_callbacks.add(key)

    def is_ready(self, key):
        """Returns true if a result is available for ``key``."""
        if key not in self.pending_callbacks:
            raise UnknownKeyError("key %r is not pending" % (key,))
        return key in self.results

    def set_result(self, key, result):
        """Sets the result for ``key`` and attempts to resume the generator."""
        self.results[key] = result
        if self.yield_point is not None and self.yield_point.is_ready():
            try:
                self.future.set_result(self.yield_point.get_result())
            except:
                self.future.set_exc_info(sys.exc_info())
            self.yield_point = None
            self.run()

    def pop_result(self, key):
        """Returns the result for ``key`` and unregisters it."""
        self.pending_callbacks.remove(key)
        return self.results.pop(key)

    def run(self):
        """Starts or resumes the generator, running until it reaches a
        yield point that is not ready.
        """
        if self.running or self.finished:
            return
        try:
            self.running = True
            while True:
                if self.exc_info is None:
                    try:
                        if not self.future.done():
                            return
                        next = self.future.result()
                        self.future = None
                    except Exception:
                        self.exc_info = sys.exc_info()
                try:
                    orig_stack_contexts = stack_context._state.contexts
                    if self.exc_info is not None:
                        self.had_exception = True
                        exc_info = self.exc_info
                        self.exc_info = None
                        yielded = self.gen.throw(*exc_info)
                    else:
                        yielded = self.gen.send(next)
                    if stack_context._state.contexts is not orig_stack_contexts:
                        self.gen.throw(
                            stack_context.StackContextInconsistentError(
                                'stack_context inconsistency (probably caused '
                                'by yield within a "with StackContext" block)'))
                except (StopIteration, Return) as e:
                    self.finished = True
                    self.future = _null_future
                    if self.pending_callbacks and not self.had_exception:
                        # If we ran cleanly without waiting on all callbacks
                        # raise an error (really more of a warning).  If we
                        # had an exception then some callbacks may have been
                        # orphaned, so skip the check in that case.
                        raise LeakedCallbackError(
                            "finished without waiting for callbacks %r" %
                            self.pending_callbacks)
                    self.result_future.set_result(getattr(e, 'value', None))
                    self.result_future = None
                    self._deactivate_stack_context()
                    return
                except Exception:
                    self.finished = True
                    self.future = _null_future
                    self.result_future.set_exc_info(sys.exc_info())
                    self.result_future = None
                    self._deactivate_stack_context()
                    return
                if isinstance(yielded, (list, dict)):
                    yielded = Multi(yielded)
                if isinstance(yielded, YieldPoint):
                    self.future = TracebackFuture()
                    def start_yield_point():
                        try:
                            yielded.start(self)
                            if yielded.is_ready():
                                self.future.set_result(
                                    yielded.get_result())
                            else:
                                self.yield_point = yielded
                        except Exception:
                            self.exc_info = sys.exc_info()
                    if self.stack_context_deactivate is None:
                        # Start a stack context if this is the first
                        # YieldPoint we've seen.
                        with stack_context.ExceptionStackContext(
                                self.handle_exception) as deactivate:
                            self.stack_context_deactivate = deactivate
                            def cb():
                                start_yield_point()
                                self.run()
                            self.io_loop.add_callback(cb)
                            return
                    else:
                        start_yield_point()
                elif is_future(yielded):
                    self.future = yielded
                    if not self.future.done():
                        self.io_loop.add_future(
                            self.future, lambda f: self.run())
                else:
                    self.exc_info = (BadYieldError(
                        "yielded unknown object %r" % (yielded,)),)
        finally:
            self.running = False

    def result_callback(self, key):
        def inner(*args, **kwargs):
            if kwargs or len(args) > 1:
                result = Arguments(args, kwargs)
            elif args:
                result = args[0]
            else:
                result = None
            self.set_result(key, result)
        return stack_context.wrap(inner)

    def handle_exception(self, typ, value, tb):
        if not self.running and not self.finished:
            self.exc_info = (typ, value, tb)
            self.run()
            return True
        else:
            return False

    def _deactivate_stack_context(self):
        if self.stack_context_deactivate is not None:
            self.stack_context_deactivate()
            self.stack_context_deactivate = None

Arguments = collections.namedtuple('Arguments', ['args', 'kwargs'])
