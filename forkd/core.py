"""
Pre-forking process manager.
"""


import errno
import fcntl
import logging
import os
import signal
import sys


# Signals trapped and their single-byte identifier when sent through pipe.
SIGNAL_IDS = {
    'SIGCHLD': 'C',
    'SIGHUP':  'H',
    'SIGINT':  'I',
    'SIGQUIT': 'Q',
    'SIGUSR1': '1',
    'SIGUSR2': '2',
    'SIGTERM': 'T',
}
SIGNAL_IDS_REV = dict((v, k) for (k, v) in SIGNAL_IDS.iteritems())


# Worker messages.
WORKER_QUIT = 'Q'


class Forkd(object):
    """Pre-forking process manager.
    """

    def __init__(self, worker_func, num_workers=1):
        self.worker_func = worker_func
        self.num_workers = num_workers
        self._status = None
        self._signal_pipe = None
        self._workers = {}
        self._log = logging.getLogger('forkd')

    def run(self):
        """Run workers and block until no workers remain.
        """
        self._status = 'starting'
        self._setup()
        self._spawn_workers()
        self._status = 'running'
        self._loop()
        self._status = 'ended'

    def _shutdown(self):
        """Shutdown workers cleanly.
        """
        # Ignore if already shutting down.
        if self._status == 'shutdown':
            return
        self._log.info('[%s] shutting down', os.getpid())
        self._status = 'shutdown'
        # Set num_workers to 0 to avoid spawning any more children.
        self.num_workers = 0
        # Shutdown workers.
        self._shutdown_workers()

    def _shutdown_workers(self):
        """Safely shutdown all workers.
        """
        for pid in self._workers:
            self._shutdown_worker(pid)

    def _shutdown_worker(self, pid):
        """Safely shutdown the worker with given pid.
        """
        worker = self._workers[pid]
        if worker['status'] != 'running':
            return
        worker['status'] = 'shutdown'
        os.write(worker['pipe'][1], WORKER_QUIT)

    def _loop(self):
        """Loop, handling signals, until no workers exist.
        """
        f = os.fdopen(self._signal_pipe[0], 'r')
        while self._workers:
            try:
                msg = f.readline()
                if not msg:
                    break
                # Parse message
                signal_id, from_pid = msg.strip().split()
                from_pid = int(from_pid)
                # Call signal handler.
                handler = getattr(self, '_' + SIGNAL_IDS_REV[signal_id])
                handler(from_pid)
            except IOError, e:
                if e.errno != errno.EINTR:
                    self._log.info('IOError %x: %s', e.errno, unicode(e))
                    raise
            except Exception:
                self._log.exception('Unexpected exception in master process loop')
                raise

    def _setup(self):
        """Setup signals and master control pipe.
        """
        self._signal_pipe = os.pipe()
        for name in SIGNAL_IDS:
            self._signal(name)

    def _spawn_workers(self):
        """Spawn required number of worker processes.
        """
        for i in range(max(self.num_workers - len(self._workers), 0)):
            pid, pipe = self._spawn_worker()
            self._workers[pid] = {'pipe': pipe, 'status': 'running'}
            self._log.info('[%s] started worker %s', os.getpid(), pid)

    def _respawn_workers(self):
        """Respawn all worker processes.
        """
        self._log.info('[%s] respawning workers', os.getpid())
        for pid, worker in self._workers.iteritems():
            if worker['status'] == 'running':
                self._shutdown_worker(pid)

    def _spawn_worker(self):
        """Spawn a single worker process.
        """

        # Create worker control pipe. Read end is non-blocking so we can "peek" at it.
        worker_pipe = os.pipe()
        fcntl.fcntl(worker_pipe[0], fcntl.F_SETFL, fcntl.fcntl(worker_pipe[0], fcntl.F_GETFL) | os.O_NONBLOCK)

        # Fork process and return immediately if not new worker.
        pid = os.fork()
        if pid:
            return pid, worker_pipe

        # Get worker pid.
        pid = os.getpid()
        self._log.debug('[%s] worker running', pid)

        # Create worker.
        worker = _resolve_worker(self.worker_func)()

        # Loop until either the worker ends or is shutdown.
        while True:
            # Read byte from worker pipe, if available.
            try:
                ch = os.read(worker_pipe[0], 1)
            except OSError, e:
                if e.errno != errno.EAGAIN:
                    raise
            else:
                if ch == WORKER_QUIT:
                    self._log.debug('[%s] received QUIT', pid)
                    break
            # Run worker.
            try:
                worker.next()
            except StopIteration:
                break
            except Exception, e:
                self._log.exception('[%s] exception in worker', pid)
                sys.exit(-1)

        # Exit worker process.
        self._log.debug('[%s] worker ending', pid)
        sys.exit(0)

    def _add_worker(self):
        """Add a new worker process.
        """
        self.num_workers += 1
        self._log.info('[%s] adding worker, num_workers=%d', os.getpid(), self.num_workers)
        self._spawn_workers()

    def _remove_worker(self):
        """Remove a worker process.
        """
        if self.num_workers <= 1:
            return
        self.num_workers -= 1
        self._log.info('[%s] removing worker, num_workers=%d', os.getpid(), self.num_workers)
        for pid, worker in self._workers.iteritems():
            if worker['status'] == 'running':
                self._shutdown_worker(pid)
                break

    def _signal(self, signame):
        """Install signal handler that routes the signal event to the pipe.
        """
        signal_id = SIGNAL_IDS[signame]
        def handler(signo, frame):
            self._log.debug('[%d] signal: %s', os.getpid(), signame)
            os.write(self._signal_pipe[1], '%s %s\n' % (signal_id, os.getpid()))
        signal.signal(getattr(signal, signame), handler)

    def _SIGCHLD(self, from_pid):
        """Handle child termination.
        """
        self._log.debug('[%s] SIGCHLD', os.getpid())
        while self._workers:
            pid, status = os.waitpid(-1, os.WNOHANG)
            if not pid:
                break
            status = status >> 8
            self._log.info('[%s] worker %s ended with status: %s', os.getpid(), pid, status)
            worker = self._workers.pop(pid)
            os.close(worker['pipe'][0])
            os.close(worker['pipe'][1])
        self._spawn_workers()

    def _SIGHUP(self, from_pid):
        """Handle HUP interrupt.
        """
        self._log.debug('[%s] SIGHUP from %d', os.getpid(), from_pid)
        if from_pid == os.getpid():
            self._respawn_workers()
        else:
            self._shutdown_worker(from_pid)

    def _SIGINT(self, from_pid):
        """Handle terminal interrupt.
        """
        self._log.debug('[%s] SIGINT from %d', os.getpid(), from_pid)
        if from_pid == os.getpid():
            self._shutdown()
        else:
            self._shutdown_worker(from_pid)

    def _SIGQUIT(self, from_pid):
        """Handle quit interrupt.
        """
        self._log.debug('[%s] SIGQUIT from %d', os.getpid(), from_pid)
        if from_pid == os.getpid():
            self._shutdown()
        else:
            self._shutdown_worker(from_pid)

    def _SIGTERM(self, from_pid):
        """Handle termination request.
        """
        self._log.debug('[%s] SIGTERM from %d', os.getpid(), from_pid)
        if from_pid == os.getpid():
            self._shutdown()
        else:
            self._shutdown_worker(from_pid)

    def _SIGUSR1(self, from_pid):
        """Handle usr1 (add worker) request.
        """
        self._log.debug('[%s] SIGUSR1 from %d', os.getpid(), from_pid)
        if from_pid == os.getpid():
            self._add_worker()

    def _SIGUSR2(self, from_pid):
        """Handle usr2 (remove worker) request.
        """
        self._log.debug('[%s] SIGUSR2 from %d', os.getpid(), from_pid)
        if from_pid == os.getpid():
            self._remove_worker()


def _resolve_worker(worker):
    """Resolve the worker into a callable.
    """

    # If it's not a string then assume it's already callable.
    if not isinstance(worker, basestring):
        return worker

    # Locate the callable.
    module_name, func_name = worker.split(':')
    module = __import__(module_name)
    for name in module_name.split('.')[1:]:
        module = getattr(module, name)
    return getattr(module, func_name)
