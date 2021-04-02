# Copyright (c) 2009-2012, Andrew McNabb

from errno import EINTR
from subprocess import Popen, PIPE
import os
import signal
import sys
import time
import traceback

from psshlib import askpass_client
from psshlib import color

BUFFER_SIZE = 1 << 16


try:
    bytes
except NameError:
    bytes = str


DEFAULT_ENCODING = 'utf-8'
OUTPUT_FORMATS = {
    'err': '%(host)s ' + color.B(color.r('=>')) + ' %(line)s',
    'out': '%(host)s ' + color.b(color.g('->')) + ' %(line)s',
    '': '%(host)s ' + color.B(color.b('->')) + ' %(line)s',
    # 'eco': '%(host)s =: %(line)s',  # exit code OK
    # 'ece': '%(host)s =: %(line)s',  # exit code error
}
OUTPUT_FORMATS = {key: val.encode(DEFAULT_ENCODING) for key, val in OUTPUT_FORMATS.items()}
UNTERMINATED_LINE_MARK = color.b(color.y('\\')).encode(DEFAULT_ENCODING)


class Task(object):
    '''
    Starts a process and manages its input and output.

    Upon completion, the `exitstatus` attribute is set to the exit status
    of the process.
    '''

    def __init__(self, host, port, user, cmd, opts, stdin=None):
        self.exitstatus = None

        self.host = host
        self.host_b = host.encode(DEFAULT_ENCODING)
        self.pretty_host = host
        self.port = port
        self.cmd = cmd

        if user != opts.user:
            self.pretty_host = '@'.join((user, self.pretty_host))
        if port:
            self.pretty_host = ':'.join((self.pretty_host, port))

        self.proc = None
        self.writer = None
        self.timestamp = None
        self.failures = []
        self.killed = False
        self.inputbuffer = stdin
        self.byteswritten = 0
        self.outputbuffer = bytes()
        self.fd_to_buffer = {}
        self.errorbuffer = bytes()

        self.stdin = None
        self.stdout = None
        self.stderr = None
        self.outfile = None
        self.errfile = None
        try:
            self.outstream = sys.stdout.buffer
            self.errstream = sys.stderr.buffer
        except AttributeError:  # PY2
            self.outstream = sys.stdout
            self.errstream = sys.stderr

        # Set options.
        self.verbose = opts.verbose
        try:
            self.print_out = bool(opts.print_out)
        except AttributeError:
            self.print_out = False
        try:
            self.inline = bool(opts.inline)
        except AttributeError:
            self.inline = False
        try:
            self.annotate_lines = not bool(opts.no_annotate_lines)
        except AttributeError:
            self.annotate_lines = True
        try:
            self.buffer_lines = not bool(opts.no_buffer_lines)
        except AttributeError:
            self.buffer_lines = True
        try:
            self.inline_stdout = bool(opts.inline_stdout)
        except AttributeError:
            self.inline_stdout = False

    def start(self, nodenum, iomap, writer, askpass_socket=None):
        ''' Starts the process and registers files with the IOMap. '''
        self.writer = writer

        if writer:
            self.outfile, self.errfile = writer.open_files(self.pretty_host)

        # Set up the environment.
        environ = dict(os.environ)
        environ['PSSH_NODENUM'] = str(nodenum)
        environ['PSSH_HOST'] = self.host
        # Disable the GNOME pop-up password dialog and allow ssh to use
        # askpass.py to get a provided password.  If the module file is
        # askpass.pyc, we replace the extension.
        environ['SSH_ASKPASS'] = askpass_client.executable_path()
        if askpass_socket:
            environ['PSSH_ASKPASS_SOCKET'] = askpass_socket
        if self.verbose:
            environ['PSSH_ASKPASS_VERBOSE'] = '1'
        # Work around a mis-feature in ssh where it won't call SSH_ASKPASS
        # if DISPLAY is unset.
        if 'DISPLAY' not in environ:
            environ['DISPLAY'] = 'pssh-gibberish'

        # Create the subprocess.  Since we carefully call set_cloexec() on
        # all open files, we specify close_fds=False.
        self.proc = Popen(self.cmd, stdin=PIPE, stdout=PIPE, stderr=PIPE,
                close_fds=False, preexec_fn=os.setsid, env=environ)
        self.timestamp = time.time()
        if self.inputbuffer:
            self.stdin = self.proc.stdin
            iomap.register_write(self.stdin.fileno(), self.handle_stdin)
        else:
            self.proc.stdin.close()
        self.stdout = self.proc.stdout
        iomap.register_read(self.stdout.fileno(), self.handle_stdout)
        self.stderr = self.proc.stderr
        iomap.register_read(self.stderr.fileno(), self.handle_stderr)

    def _kill(self):
        ''' Signals the process to terminate. '''
        if self.proc:
            try:
                os.kill(-self.proc.pid, signal.SIGKILL)
            except OSError:
                # If the kill fails, then just assume the process is dead.
                pass
            self.killed = True

    def timedout(self):
        ''' Kills the process and registers a timeout error. '''
        if not self.killed:
            self._kill()
            self.failures.append('Timed out')

    def interrupted(self):
        ''' Kills the process and registers an keyboard interrupt error. '''
        if not self.killed:
            self._kill()
            self.failures.append('Interrupted')

    def cancel(self):
        ''' Stops a task that has not started. '''
        self.failures.append('Cancelled')

    def elapsed(self):
        ''' Finds the time in seconds since the process was started. '''
        return time.time() - self.timestamp

    def running(self):
        ''' Finds if the process has terminated and saves the return code. '''
        if self.stdin or self.stdout or self.stderr:
            return True
        if self.proc:
            self.exitstatus = self.proc.poll()
            if self.exitstatus is None:
                if self.killed:
                    # Set the exitstatus to what it would be if we waited.
                    self.exitstatus = -signal.SIGKILL
                    return False
                else:
                    return True
            else:
                if self.exitstatus < 0:
                    message = 'Killed by signal %s' % (-self.exitstatus)
                    self.failures.append(message)
                elif self.exitstatus > 0:
                    message = 'Exited with error code %s' % self.exitstatus
                    self.failures.append(message)
                self.proc = None
                return False

    def handle_stdin(self, fd, iomap):
        ''' Called when the process's standard input is ready for writing. '''
        try:
            start = self.byteswritten
            if start < len(self.inputbuffer):
                chunk = self.inputbuffer[start:start+BUFFER_SIZE]
                self.byteswritten = start + os.write(fd, chunk)
            else:
                self.close_stdin(iomap)
        except (OSError, IOError) as exc:
            ei = sys.exc_info()
            if exc.errno != EINTR:
                self.close_stdin(iomap)
                self.log_exception(exc, ei=ei)

    def close_stdin(self, iomap):
        if self.stdin:
            iomap.unregister(self.stdin.fileno())
            self.stdin.close()
            self.stdin = None

    def handle_stdout(self, fd, iomap):
        ''' Called when the process's standard output is ready for reading. '''
        try:
            buf = os.read(fd, BUFFER_SIZE)
            if buf:
                if self.inline or self.inline_stdout:
                    self.outputbuffer += buf
                if self.outfile:
                    self.writer.write(self.outfile, buf)
                if self.print_out:
                    if True:
                        self.print_annotated_lines(buf, fd=fd)
                    else:  # This is nearly uselss because it intermixes the hosts' data.
                        # self.outstream.write(b'%s: %s' % (self.host, buf))
                        self.outstream.write(buf)
                        if buf[-1] != '\n':
                            self.outstream.write(b'\n')
            else:
                if self.annotate_lines:
                    # Flush the remaining buffer
                    self.print_annotated_lines(b'', fd=fd, force_finish=True)
                self.close_stdout(iomap)
        except (OSError, IOError) as exc:
            ei = sys.exc_info()
            if exc.errno != EINTR:
                self.close_stdout(iomap)
                self.log_exception(exc)

    def close_stdout(self, iomap):
        if self.stdout:
            iomap.unregister(self.stdout.fileno())
            self.stdout.close()
            self.stdout = None
        if self.outfile:
            self.writer.close(self.outfile)
            self.outfile = None

    def handle_stderr(self, fd, iomap):
        ''' Called when the process's standard error is ready for reading. '''
        try:
            buf = os.read(fd, BUFFER_SIZE)
            if buf:
                if self.inline:
                    self.errorbuffer += buf
                if self.errfile:
                    self.writer.write(self.errfile, buf)
                if self.print_out:
                    if self.annotate_lines:
                        self.print_annotated_lines(buf, fd=fd, atype='err')
            else:
                if self.annotate_lines:
                    # Flush the remaining buffer
                    self.print_annotated_lines('', fd=fd, atype='err', force_finish=True)
                self.close_stderr(iomap)
        except (OSError, IOError) as exc:
            ei = sys.exc_info()
            if exc.errno != EINTR:
                self.close_stdin(iomap)
                self.log_exception(exc, ei=ei)

    def close_stderr(self, iomap):
        if self.stderr:
            iomap.unregister(self.stderr.fileno())
            self.stderr.close()
            self.stderr = None
        if self.errfile:
            self.writer.close(self.errfile)
            self.errfile = None

    def print_annotated_lines(self, buf, atype='out', fd=None, force_finish=False):
        if fd is not None:
            buf_pre = self.fd_to_buffer.get((fd, atype), bytes())
            self.fd_to_buffer[(fd, atype)] = bytes()
            if buf_pre:
                buf = buf_pre + buf

        lines_are_unfinished = buf and (buf[-1:] != b'\n')
        lines = buf.splitlines()  # .split('\n')

        # Quite a few conditions for putting stuff into the buffer
        if (fd is not None and lines_are_unfinished and
                self.buffer_lines and not force_finish):
            self.fd_to_buffer[(fd, atype)] = lines[-1]
            lines = lines[:-1]
            lines_are_unfinished = False

        if not lines:
            return ''

        outs = []
        sformat = OUTPUT_FORMATS.get(atype) or OUTPUT_FORMATS['']
        for idx, line in enumerate(lines):
            if self.annotate_lines:
                outline = sformat % {b'line': line, b'host': self.host_b}
            else:
                outline = line
            if idx == len(lines) - 1:  # last line
                if lines_are_unfinished:
                    # Sort-of-disambiguate
                    outline = outline + UNTERMINATED_LINE_MARK
            outs.append(outline)
        out = b'\n'.join(outs) + b'\n'
        if atype == 'err':
            outbuf = self.errstream
        else:
            outbuf = self.outstream
        outbuf.write(out)
        outbuf.flush()
        return out

    def log_exception(self, exc, ei=None):
        ''' Saves a record of the most recent exception for error reporting. '''
        if self.verbose:
            if ei is None:
                ei = sys.exc_info()
            exc_type, exc_value, exc_traceback = ei
            exc = (
                'Exception: %s, %s, %s' %
                (exc_type, exc_value, traceback.format_tb(exc_traceback)))
        else:
            exc = str(exc)
        self.failures.append(exc)

    def report(self, n):
        """Pretty prints a status report after the Task completes."""
        error = ', '.join(self.failures)
        tstamp = time.asctime().split()[3]  # Current time
        if color.has_colors(self.outstream):
            progress = color.c("[%s]" % color.B(n))
            success = color.g("[%s]" % color.B("SUCCESS"))
            failure = color.r("[%s]" % color.B("FAILURE"))
            stderr = color.r("Stderr: ")
            error = color.r(color.B(error))
        else:
            progress = "[%s]" % n
            success = "[SUCCESS]"
            failure = "[FAILURE]"
            stderr = "Stderr: "
        host = self.pretty_host
        if self.failures:
            msg = ' '.join((progress, tstamp, failure, host, error))
        else:
            msg = ' '.join((progress, tstamp, success, host))
        self.errstream.write(msg.encode(DEFAULT_ENCODING) + b'\n')
        self.errstream.flush()
        # NOTE: The extra flushes are to ensure that the data is output in
        # the correct order with the C implementation of io.
        if self.outputbuffer:
            self.outstream.flush()
            self.outstream.write(self.outputbuffer)
            self.outstream.flush()
        if self.errorbuffer:
            self.outstream.write(stderr)
            # Flush the TextIOWrapper before writing to the binary buffer.
            self.outstream.flush()
            self.outstream.write(self.errorbuffer)
