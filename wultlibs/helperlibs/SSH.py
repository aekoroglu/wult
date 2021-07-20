# -*- coding: utf-8 -*-
# vim: ts=4 sw=4 tw=100 et ai si
#
# Copyright (C) 2014-2020 Intel Corporation
# SPDX-License-Identifier: BSD-3-Clause
#
# Author: Artem Bityutskiy <artem.bityutskiy@linux.intel.com>

"""
This module provides helpful API for communicating and working with remote hosts over SSH.

There are two ways we run commands remotely over SSH: in a new paramiko SSH session, and in the
interactive shell. The latter way adds complexity, and the only reason we have it is because it is
faster to run a process this way.

The first way of running commands is very straight-forward - we just open a new paramiko SSH session
and run the command. The session gets closed when the command finishes. The next command requires a
new session. Creating a new SSH session takes time, so if we need to run many commands, the overhead
becomes very noticeable.

The second way of running commands is via the interactive shell. We run the 'sh -s' process in a new
paramiko session, and then just run commands in this shell. One command can run at a time. But we do
not need to create a new SSH session between the commands. The complication with this method is to
detect when command has finished. We solve this problem my making each command print a unique random
hash to 'stdout' when it finishes.

SECURITY NOTICE: this module and any part of it should only be used for debugging and development
purposes. No security audit had been done. Not for production use.
"""

# pylint: disable=no-member
# pylint: disable=protected-access

import os
import re
import pwd
import glob
import time
import stat
import types
import shlex
import queue
import codecs
import socket
import random
import logging
import threading
from pathlib import Path
import contextlib
import paramiko
from wultlibs.helperlibs import _Common, Procs, WrapExceptions, Trivial
from wultlibs.helperlibs._Common import ProcResult, cmd_failed_msg # pylint: disable=unused-import
from wultlibs.helperlibs._Common import TIMEOUT
from wultlibs.helperlibs.Exceptions import Error, ErrorPermissionDenied, ErrorTimeOut, ErrorConnect

_LOG = logging.getLogger()

# Paramiko is a bit too noisy, lower its log level.
logging.getLogger("paramiko").setLevel(logging.WARNING)

# The exceptions to handle when dealing with paramiko.
_PARAMIKO_EXCEPTIONS = (OSError, IOError, paramiko.SSHException, socket.error)

def _stream_fetcher(streamid, chan):
    """
    This function runs in a separate thread. All it does is it fetches one of the output streams
    of the executed program (either stdout or stderr) and puts the result into the queue.
    """

    pd = chan._pd_
    read_func = pd.streams[streamid]
    decoder = codecs.getincrementaldecoder('utf8')(errors="surrogateescape")

    try:
        while not pd.threads_exit:
            if not read_func:
                chan._dbg_("stream %d: stream is closed", streamid)
                break

            data = None
            try:
                data = read_func(4096)
            except socket.timeout:
                chan._dbg_("stream %d: read timeout", streamid)
                continue

            if not data:
                chan._dbg_("stream %d: no more data", streamid)
                break

            data = decoder.decode(data)
            if not data:
                chan._dbg_("stream %d: read more data", streamid)
                continue

            chan._dbg_("stream %d: read data:\n%s", streamid, data)
            pd.queue.put((streamid, data))
    except BaseException as err: # pylint: disable=broad-except
        _LOG.error(err)

    # The end of stream indicator.
    pd.queue.put((streamid, None))
    chan._dbg_("stream %d: thread exists", streamid)

def _recv_exit_status_timeout(chan, timeout):
    """
    This is a version of paramiko channel's 'recv_exit_status()' which supports a timeout.
    Returns the exit status or 'None' in case of 'timeout'.
    """

    chan._dbg_("_recv_exit_status_timeout: waiting for exit status, timeout %s sec", timeout)

#    This is non-hacky, but polling implementation.
#    if timeout:
#        start_time = time.time()
#        while not chan.exit_status_ready():
#            if time.time() - start_time > timeout:
#                chan._dbg_("exit status not ready for %s seconds", timeout)
#                return None
#            time.sleep(1)
#    exitcode = chan.recv_exit_status()

    # This is hacky, but non-polling implementation.
    if not chan.status_event.wait(timeout=timeout):
        chan._dbg_("_recv_exit_status_timeout: exit status not ready for %s seconds", timeout)
        return None

    exitcode = chan.exit_status
    chan._dbg_("_recv_exit_status_timeout: exit status %d", exitcode)
    return exitcode

def _watch_for_marker(chan, data):
    """
    When we run a command in the interactive shell (as opposed to running in a dedicated SSH
    session), the way we can detect that the command has ended is by watching for a special marker
    in 'stdout' of the interactive shell process.

    This is a helper for '_do_wait_for_cmd_intsh()' which takes a piece of 'stdout' data that came
    from the stream fetcher and checks for the marker in it. Returns a tuple of '(cdata, exitcode)',
    where 'cdata' is the stdout data that has to be captured, and exitcode is the exit code of the
    command.

    In other words, if no marker was not found, this function returns '(cdata, None)', and 'cdata'
    may not be the same as 'data', because part of it may be saved in 'pd.ll', because it looks
    like the beginning of the marker. I marker was found, this function returns '(cdata, exitcode)'.
    Again, 'cdata' does not have to be the same as 'data', because 'data' could contain the marker,
    which will not be present in 'cdata'. The 'exitcode' will contain an integer exit code of the
    command.
    """

    pd = chan._pd_
    exitcode = None
    cdata = None

    chan._dbg_("_watch_for_marker: starting with pd.check_ll %s, pd.ll: %s, data:\n%s",
               str(pd.check_ll), str(pd.ll), data)

    split = data.rsplit("\n", 1)
    if len(split) > 1:
        # We have got a new line. This is our new marker suspect. Keep it in 'pd.ll', while old
        # 'pd.ll' and the rest of 'data' can be returned up for capturing. Set 'pd.check_ll' to
        # 'True' to indicate that 'pd.ll' has to be checked for the marker.
        cdata = pd.ll + split[0] + "\n"
        pd.check_ll = True
        pd.ll = split[1]
    else:
        # We have got a continuation of the previous line. The 'check_ll' flag is 'True' when
        # 'pd.ll' being a marker is a real possibility. If we already checked 'pd.ll' and it
        # starts with data that is different to the marker, there is not reason to check it again,
        # and we can send it up for capturing.
        if not pd.ll:
            pd.check_ll = True
        if pd.check_ll:
            pd.ll += split[0]
            cdata = ""
        else:
            cdata = pd.ll + data
            pd.ll = ""

    if pd.check_ll:
        # 'pd.ll' is a real suspect, check if it looks like the marker.
        pd.check_ll = pd.ll.startswith(pd.marker) or pd.marker.startswith(pd.ll)

    # OK, if 'pd.ll' is still a real suspect, do a full check using the regex: full marker line
    # should contain not only the hash, but also the exit status.
    if pd.check_ll and re.match(pd.marker_regex, pd.ll):
        # Extract the exit code from the 'pd.ll' string that has the following form:
        # --- hash, <exitcode> ---
        split = pd.ll.rsplit(", ", 1)
        assert len(split) == 2
        exitcode = split[1].rstrip(" ---")
        if not Trivial.is_int(exitcode):
            raise Error(f"the command was running{pd.ssh.hostmsg} under the interactive "
                        f"shell and finished with a correct marker, but unexpected exit "
                        f"code '{exitcode}'.\nThe command was: {chan.cmd}")

        pd.ll = ""
        pd.check_ll = False
        exitcode = int(exitcode)

    chan._dbg_("_watch_for_marker: ending with pd.check_ll %s, pd.ll %s, exitcode %s, cdata:\n%s",
               str(pd.check_ll), pd.ll, str(exitcode), cdata)

    return (cdata, exitcode)

def _have_enough_lines(output, lines=(None, None)):
    """Returns 'True' if there are enough lines in the output buffer."""

    for streamid in (0, 1):
        if lines[streamid] and len(output[streamid]) >= lines[streamid]:
            return True
    return False

def _do_wait_for_cmd_intsh(chan, timeout=None, capture_output=True, output_fobjs=(None, None),
                           lines=(None, None), by_line=True):
    """
    Implements '_wait_for_cmd()' for the optimized case when the command was executed in the
    interactive shell process. This case allows us to save time on creating a separate session for
    commands.
    """

    pd = chan._pd_
    output = pd.output
    partial = pd.partial
    start_time = time.time()

    chan._dbg_("_do_wait_for_cmd_intsh: starting with pd.check_ll %s, pd.ll: %s, "
               "pd.partial: %s, pd.output:\n%s",
               str(pd.check_ll), str(pd.ll), partial, str(output))

    while not _have_enough_lines(output, lines=lines):
        if pd.exitcode is not None and pd.queue.empty():
            chan._dbg_("_do_wait_for_cmd_intsh: process exited with status %d", pd.exitcode)
            break

        streamid, data = _Common.get_next_queue_item(pd.queue, timeout)
        if streamid == -1:
            chan._dbg_("_do_wait_for_cmd_intsh: nothing in the queue for %d seconds", timeout)
        elif data is None:
            raise Error(f"the interactive shell process{pd.ssh.hostmsg} closed stream "
                        f"'{pd.streams[streamid]._stream_name}' while running the following "
                        f"command:\n{chan.cmd}")
        elif streamid == 0:
            # The indication that the command has ended is our marker in stdout (stream 0). Our goal
            # is to watch for this marker, hide it from the user, because it does not belong to the
            # output of the command. The marker always starts at the beginning of line.
            data, pd.exitcode = _watch_for_marker(chan, data)

        _Common.capture_data(chan, streamid, data, capture_output=capture_output,
                             output_fobjs=output_fobjs, by_line=by_line)

        if not timeout:
            chan._dbg_(f"_do_wait_for_cmd_intsh: timeout is {timeout}, exit immediately")
            break
        if time.time() - start_time > timeout:
            chan._dbg_("_do_wait_for_cmd_intsh: stop waiting for the command - timeout")
            break

    result = _Common.get_lines_to_return(chan, lines=lines)

    if _Common.all_output_consumed(chan):
        # Mark the interactive shell process as vacant.
        acquired = pd.ssh._acquire_intsh_lock(chan.cmd)
        if not acquired:
            _LOG.warning("failed to mark the interactive shell process as free")
        else:
            pd.ssh._intsh_busy = False
            pd.ssh._intsh_lock.release()

    return result

def _do_wait_for_cmd(chan, timeout=None, capture_output=True, output_fobjs=(None, None),
                     lines=(None, None), by_line=True):
    """
    Implements '_wait_for_cmd()' for the non-optimized case when the command was executed in its own
    separate SSH session.
    """

    pd = chan._pd_
    output = pd.output
    partial = pd.partial
    start_time = time.time()

    chan._dbg_("_do_wait_for_cmd: starting with partial: %s, output:\n%s", partial, str(output))

    while not _have_enough_lines(output, lines=lines):
        if pd.exitcode is not None:
            chan._dbg_("_do_wait_for_cmd: process exited with status %d", pd.exitcode)
            break

        streamid, data = _Common.get_next_queue_item(pd.queue, timeout)
        if streamid == -1:
            chan._dbg_("_do_wait_for_cmd_intsh: nothing in the queue for %d seconds", timeout)
        elif data is not None:
            _Common.capture_data(chan, streamid, data, capture_output=capture_output,
                                 output_fobjs=output_fobjs, by_line=by_line)
        else:
            chan._dbg_("_do_wait_for_cmd: stream %d closed", streamid)
            # One of the output streams closed.
            pd.threads[streamid].join()
            pd.threads[streamid] = pd.streams[streamid] = None

            if not pd.streams[0] and not pd.streams[1]:
                chan._dbg_("_do_wait_for_cmd: both streams closed")
                pd.exitcode = _recv_exit_status_timeout(chan, timeout)
                break

        if not timeout:
            chan._dbg_(f"_do_wait_for_cmd: timeout is {timeout}, exit immediately")
            break
        if time.time() - start_time > timeout:
            chan._dbg_("_do_wait_for_cmd: stop waiting for the command - timeout")
            break

    return _Common.get_lines_to_return(chan, lines=lines)

def _wait_for_cmd(chan, timeout=None, capture_output=True, output_fobjs=(None, None),
                  lines=(None, None), by_line=True, join=True):
    """
    This function waits for a command executed with the 'SSH.run_async()' function to finish or
    print something to stdout or stderr.

    The optional 'timeout' argument specifies the longest time in seconds this function will wait
    for the command to finish. If the command does not finish, this function exits and returns
    'None' as the exit code of the command. The timeout must be a positive floating point number. By
    default it is 1 hour. If 'timeout' is '0', then this function will just check process status,
    grab its output, if any, and return immediately.

    Note, this function saves the used timeout in 'chan.timeout' attribute upon exit.

    The 'capture_output' parameter controls whether the output of the command should be collected or
    not. If it is not 'True', the output will simply be discarded and this function will return
    empty strings instead of command's stdout and stderr.

    The 'output_fobjs' parameter is a tuple with two file-like objects where the stdout and stderr
    of the command will be echoed, in addition to being captured and returned. If not specified,
    then the the command output will not be echoed anywhere.

    The 'lines' argument provides a capability to wait for the command to output certain amount of
    lines. By default, there is no limit, and this function will wait either for timeout or until
    the command exits. The 'line' argument is a tuple, the first element of the tuple is the
    'stdout' limit, the second is the 'stderr' limit. For example, 'lines=(1, 5)' would mean to wait
    for one full line in 'stdout' or five full lines in 'stderr'. And 'lines=(1, None)' would mean
    to wait for one line in 'stdout' and any amount of lines in 'stderr'.

    The 'by_line' parameter controls whether this function should capture output on a line-by-line
    basis, or if it does not need to care about lines.

    The 'join' argument controls whether the captured output lines should be joined and returned as
    a single string, or no joining is needed and the output should be returned as a list of strings.
    In the latter case if if 'by_line' is 'True', the output will be a list of full lines, otherwise
    it may be a list of partial lines. Newlines are not stripped in any case.

    This function returns a named tuple similar to what the 'run()' function returns.
    """

    if timeout is None:
        timeout = TIMEOUT
    if timeout < 0:
        raise Error(f"bad timeout value {timeout}, must be > 0")

    if not by_line and lines != (None, None):
        raise Error("'by_line' must be 'True' when 'lines' is used (reading limited amount of "
                    "output lines)")

    for streamid in (0, 1):
        if not lines[streamid]:
            continue
        if not Trivial.is_int(lines[streamid]):
            raise Error("the 'lines' argument can only include integers and 'None'")
        if lines[streamid] < 0:
            raise Error("the 'lines' argument cannot include negative values")

    if lines[0] == 0 and lines[1] == 0:
        raise Error("the 'lines' argument cannot be (0, 0)")

    pd = chan._pd_
    chan.timeout = timeout

    chan._dbg_("_wait_for_cmd: timeout %s, capture_output %s, lines: %s, by_line %s, "
               "join: %s, command: %s\nreal command: %s",
               timeout, capture_output, str(lines), by_line, join, chan.cmd, pd.real_cmd)

    if pd.threads_exit:
        raise Error("this SSH channel has '_threads_exit_ flag set and it cannot be used")

    if _Common.all_output_consumed(chan):
        return ProcResult(stdout="", stderr="", exitcode=pd.exitcode)

    if not pd.queue:
        pd.queue = queue.Queue()
        for streamid in (0, 1):
            if pd.streams[streamid]:
                assert pd.threads[streamid] is None
                pd.threads[streamid] = threading.Thread(target=_stream_fetcher,
                                                         name='SSH-stream-fetcher',
                                                         args=(streamid, chan), daemon=True)
                pd.threads[streamid].start()
    else:
        chan._dbg_("_wait_for_cmd: queue is empty: %s", pd.queue.empty())

    if chan == pd.ssh._intsh:
        func = _do_wait_for_cmd_intsh
    else:
        func = _do_wait_for_cmd

    output = func(chan, timeout=timeout, capture_output=capture_output, output_fobjs=output_fobjs,
                  lines=lines, by_line=by_line)

    stdout = stderr = ""
    if output[0]:
        stdout = output[0]
        if join:
            stdout = "".join(stdout)
    if output[1]:
        stderr = output[1]
        if join:
            stderr = "".join(stderr)

    if _Common.all_output_consumed(chan):
        exitcode = pd.exitcode
    else:
        exitcode = None

    if chan._pd_.debug:
        sout = "".join(output[0])
        serr = "".join(output[1])
        chan._dbg_("_wait_for_cmd: returning, exitcode %s, stdout:\n%s\nstderr:\n%s",
                   exitcode, sout.rstrip(), serr.rstrip())

    return ProcResult(stdout=stdout, stderr=stderr, exitcode=exitcode)

def _poll(chan):
    """
    Check if the process is still running. If it is, return 'None', else return exit status.
    """

    if chan.exit_status_ready():
        return chan.recv_exit_status()
    return None

def _cmd_failed_msg(chan, stdout, stderr, exitcode, startmsg=None, timeout=None):
    """
    A wrapper over '_Common.cmd_failed_msg()'. The optional 'timeout' argument specifies the
    timeout that was used for the command.
    """

    if timeout is None:
        timeout = chan.timeout

    cmd = chan.cmd
    if _LOG.getEffectiveLevel() == logging.DEBUG:
        if chan.cmd != chan._pd_.real_cmd:
            cmd = f"{cmd}\nReal command: {chan._pd_.real_cmd}"

    return _Common.cmd_failed_msg(cmd, stdout, stderr, exitcode, hostname=chan.hostname,
                                  startmsg=startmsg, timeout=timeout)

def _close(chan):
    """The channel close method that will signal the threads to exit."""

    if hasattr(chan, "_pd_"):
        chan._dbg_("_close()")
        pd = chan._pd_
        pd.threads_exit = True
        pd.ssh = None
        pd.orig_close()

def _del(chan):
    """The channel object destructor which makes all threads to exit."""

    if hasattr(chan, "_pd_"):
        chan._dbg_("_del_()")
        chan._close()
        chan._pd_.orig_del()

def _get_err_prefix(fobj, method):
    """Return the error message prefix."""
    return f"method '{method}()' failed for {fobj._stream_name_}"

def _dbg(chan, fmt, *args):
    """Print a debugging message related to the 'chan' channel handling."""
    if chan._pd_.debug:
        pfx = ""
        if chan._pd_.debug_id:
            pfx += f"{chan._pd_.debug_id}: "
        if hasattr(chan, "pid"):
            pfx += f"PID {chan.pid}: "

        _LOG.debug(pfx + fmt, *args)

class _ChannelPrivateData:
    """
    We need to attach additional data to the paramiko channel object. This class represents that
    data.
    """

    def __init__(self):
        """The constructor."""

        # The 'SSH' object corresponding to the channel.
        self.ssh = None
        # File objects for the 3 standard streams of the command's process.
        self.stdin = None
        self.stdout = None
        self.stderr = None
        # The 2 output streams of the command's process (stdout, stderr).
        self.streams = []
        # The queue which is used for passing commands output from stream fetcher threads.
        self.queue = None
        # The threads fetching data from the output streams and placing them to the queue.
        self.threads = [None, None]
        # The threads have to exit if the 'threads_exit' flag becomes 'True'.
        self.threads_exit = False
        # Exit code of the command ('None' if it is still running).
        self.exitcode = None
        # The output for the command that was read from 'queue', but not yet sent to the user
        # (separate for 'stdout' and 'stderr').
        self.output = [[], []]
        # In case user specified 'by_line', this tuple contains the last partial lines of the
        # 'stdout' and 'stderr' output of the command. In case 'by_line' was 'False', the partial
        # lines will be in 'output'.
        self.partial = ["", ""]

        # Real command (user command and all the prefixes/suffixes).
        self.real_cmd = None
        # The original 'close()' and '__del__()' methods of the paramiko channel object.
        self.orig_close = None
        self.orig_del = None
        # Print debugging messages if 'True'.
        self.debug = False
        # Prefix debugging messages with this string. Can be useful to distinguish between debugging
        # message related to different processes.
        self.debug_id = None

def _add_custom_fields(chan, ssh, cmd, real_cmd):
    """Add a couple of custom fields to the paramiko channel object."""

    pd = chan._pd_ = _ChannelPrivateData()

    for name, mode in (("stdin", "wb"), ("stdout", "rb"), ("stderr", "rb")):
        try:
            if name != "stderr":
                fobj = chan.makefile(mode, 0)
            else:
                fobj = chan.makefile_stderr(mode, 0)
        except _PARAMIKO_EXCEPTIONS as err:
            raise Error(f"failed to create a file for '{name}': {err}") from err

        setattr(fobj, "_stream_name_", name)
        wrapped_fobj = WrapExceptions.WrapExceptions(fobj, exceptions=_PARAMIKO_EXCEPTIONS,
                                                     get_err_prefix=_get_err_prefix)
        wrapped_fobj.name = name
        setattr(pd, name, wrapped_fobj)

    pd.ssh = ssh
    pd.real_cmd = real_cmd
    pd.streams = [chan.recv, chan.recv_stderr]
    pd.orig_close = chan.close
    pd.orig_del = chan.__del__

    # The below attributes are added to make the channel object look similar to the Popen object
    # which the 'Procs' module uses.
    chan.hostname = ssh.hostname
    chan.cmd = cmd
    chan.timeout = TIMEOUT
    chan.close = types.MethodType(_close, chan)

    chan._dbg_ = types.MethodType(_dbg, chan)
    chan.poll = types.MethodType(_poll, chan)
    chan.cmd_failed_msg = types.MethodType(_cmd_failed_msg, chan)
    chan.wait_for_cmd = types.MethodType(_wait_for_cmd, chan)
    chan.__del__ = types.MethodType(_del, chan)

    return chan

def _init_intsh_custom_fields(chan, cmd, real_cmd, marker):
    """
    In case of interactive shell we carry more private data in the paramiko channel. And for every
    new command that we run in the interactive shell, we have to re-initialize some of the fields.
    """

    pd = chan._pd_

    # The marker indicating that the command has finished.
    pd.marker = marker
    # The regular expression the last line of command output should match.
    pd.marker_regex = re.compile(f"^{marker}, \\d+ ---$")
    # The command executed under the interactive shell.
    chan.cmd = cmd
    # Re real command ('pd.cmd' and all the prefixes/suffixes).
    pd.real_cmd = real_cmd
    # The last line printed by the command to stdout observed so far.
    pd.ll = ""
    # Whether the last line ('ll') should be checked against the marker. Used as an optimization in
    # order to avoid matching the 'll' against the marker too often.
    pd.check_ll = True

    pd.exitcode = None
    pd.output = [[], []]
    pd.partial = ["", ""]

def _get_username(uid=None):
    """Return username of the current process or UID 'uid'."""

    try:
        if uid is None:
            uid = os.getuid()
    except OSError as err:
        raise Error("failed to detect user name of the current process:\n%s" % err) from None

    try:
        return pwd.getpwuid(uid).pw_name
    except KeyError as err:
        raise Error("failed to get user name for UID %d:\n%s" % (uid, err)) from None

def _format_command_for_pid(command, cwd=None):
    """
    When we run a command over SSH, we do not know it's PID. But users do need it in many
    cases in order to be able finding and, for example, killing the processes they run. This
    function modifies the original user 'command' command so that it prints the PID as the first
    line of its output to the 'stdout' stream. This requires a shell.
    """

    # Prepend the command with a shell statement which prints the PID of the shell where the
    # command will be run. Then use 'exec' to make sure that the command inherits the PID.
    prefix = r'printf "%s\n" "$$";'
    if cwd:
        prefix += f""" cd "{cwd}" &&"""

    return prefix + " exec -- " + command

class SSH:
    """
    This class provides API for communicating with remote hosts over SSH.

    SECURITY NOTICE: this class and any part of it should only be used for debugging and development
    purposes. No security audit had been done. Not for production use.
    """

    Error = Error

    def _read_pid(self, chan):
        """Return PID of just executed command."""

        chan._dbg_("_read_pid: reading PID for command: %s", chan.cmd)

        timeout = 10
        stdout, stderr, _ = _wait_for_cmd(chan, timeout=timeout, lines=(1, 0), by_line=True,
                                          join=False)
        assert len(stdout) == 1
        assert not stderr

        pid = stdout[0].strip()

        if len(pid) > 128:
            raise Error(f"received too long and probably bogus PID: {pid}\n"
                        f"The command{self.hostmsg} was:\n{chan.cmd}")
        if not Trivial.is_int(pid):
            raise Error(f"received a bogus non-integer PID: {pid}\n"
                        f"The command{self.hostmsg} was:\n{chan.cmd}")

        chan._dbg_("_read_pid: PID is %s for command: %s", pid, chan.cmd)
        return int(pid)

    def _run_in_new_session(self, command, cwd=None, shell=True):
        """Run command 'command' in a new session."""

        cmd = command
        if shell:
            cmd = _format_command_for_pid(command, cwd=cwd)

        try:
            chan = self.ssh.get_transport().open_session(timeout=self.connection_timeout)
            chan.exec_command(cmd)
        except (paramiko.SSHException, socket.error) as err:
            raise Error(f"cannot execute the following command in new SSH session{self.hostmsg}:\n"
                        "{cmd}\nReason: {err}") from err

        _add_custom_fields(chan, self, command, cmd)

        if shell:
            # The first line of the output should contain the PID - extract it.
            chan.pid = self._read_pid(chan)

        return chan

    def _run_in_intsh(self, command, cwd=None):
        """Run command 'command' in the interactive shell."""

        if not self._intsh:
            cmd = "sh -s"
            _LOG.debug("starting interactive shell%s: %s", self.hostmsg, cmd)
            self._intsh = self._run_in_new_session(cmd, shell=False)

        cmd = _format_command_for_pid(command, cwd=cwd)

        # Run the command in a shell. Keep in mind, the command starts with 'exec', so it'll use the
        # shell process at the end. Once the command finishes, print its exit status and a random
        # marker specifying the end of output. This marker will be used to detect that the command
        # has finishes.
        marker = random.getrandbits(256)
        marker = f"--- {marker:064x}"
        cmd = "sh -c " + shlex.quote(cmd) + "\n" + f'printf "%s, %d ---" "{marker}" "$?"\n'
        _init_intsh_custom_fields(self._intsh, command, cmd, marker)
        self._intsh.send(cmd)

        self._intsh.pid = self._read_pid(self._intsh)
        return self._intsh

    def _acquire_intsh_lock(self, command=None):
        """
        Acquire the interactive shell lock. It should be acquired only for short period of times.
        """

        timeout = 5
        acquired = self._intsh_lock.acquire(timeout=timeout)
        if not acquired:
            msg = "failed to acquire the interactive shell lock"
            if command:
                msg += f" for for the following command:\n{command}\n"
            else:
                msg += "."
            msg += f"Waited for {timeout} seconds."
            _LOG.warning(msg)
        return acquired

    def _run_async(self, command, cwd=None, shell=True, intsh=False):
        """Implements 'run_async()'."""

        if not shell and intsh:
            raise Error("'shell' argument must be 'True' when 'intsh' is 'True'")

        if not shell or not intsh:
            return self._run_in_new_session(command, cwd=cwd, shell=shell)

        try:
            acquired = self._acquire_intsh_lock(command=command)
            if not acquired or self._intsh_busy:
                intsh = False

            if intsh:
                self._intsh_busy = True
        finally:
            # Release the interactive shell lock if we acquired it.
            if acquired:
                self._intsh_lock.release()

        if not intsh:
            _LOG.warning("interactive shell is busy, running the following command in a new "
                         "SSH session:\n%s", command)
            return self._run_in_new_session(command, cwd=cwd, shell=shell)

        try:
            return self._run_in_intsh(command, cwd=cwd)
        except:
            # Mark internal shell and free.
            with contextlib.suppress(Exception):
                acquired = self._acquire_intsh_lock(command=command)
                if acquired:
                    self._intsh_busy = False
                    self._intsh_lock.release()
                else:
                    _LOG.dbg("failed to mark the interactive shell process as free")
            raise

    def run_async(self, command, cwd=None, shell=True, intsh=False):
        """
        Run command 'command' on a remote host and return immediately without waiting for the
        command to complete.

        The 'cwd' argument is the same as in case of the 'run()' method.

        The 'shell' argument tells whether it is safe to assume that the SSH daemon on the target
        system runs some sort of Unix shell for the SSH session. Usually this is the case, but not
        always. E.g., Dell's iDRACs do not run a shell when you log into them. The reason this
        function may want to assume that the 'command' command runs in a shell is to get the PID of
        the process on the remote system. So if you do not really need to know the PID, leave the
        'shell' parameter to be 'False'.

        The 'intsh' argument indicates whether the command should run in an interactive shell or in
        a separate SSH session. The former is faster because creating a new SSH session takes time.
        However, only one command can run in an interactive shell at a time. Thereforr, by default
        'intsh' is 'False'. Note, 'shell' cannot be 'False' if 'intsh' is 'True'.

        Returns the paramiko session channel object. The object will contain an additional 'pid'
        attribute, and depending on the 'shell' parameter, the attribute will have value 'None'
        ('shell' is 'False') or the integer PID of the executed process on the remote host ('shell'
        is 'True').
        """

        # Allow for 'command' to be a 'pathlib.Path' object which Paramiko does not accept.
        command = str(command)

        if cwd:
            if not shell:
                raise Error("cannot set working directory to '{cwd}' - using shell is disallowed")
            cwd_msg = f"\nWorking directory: {cwd}"
        else:
            cwd_msg = ""
        _LOG.debug("running the following command asynchronously%s (shell %s, intsh %s):\n%s%s",
                   self.hostmsg, str(shell), str(intsh), command, cwd_msg)

        return self._run_async(str(command), cwd=cwd, shell=shell, intsh=intsh)

    def run(self, command, timeout=None, capture_output=True, mix_output=False, join=True,
            output_fobjs=(None, None), cwd=None, shell=True, intsh=None): # pylint: disable=unused-argument
        """
        Run command 'command' on the remote host and block until it finishes. The 'command' argument
        should be a string.

        The 'timeout' parameter specifies the longest time for this method to block. If the command
        takes longer, this function will raise the 'ErrorTimeOut' exception. The default is 1h.

        If the 'capture_output' argument is 'True', this function intercept the output of the
        executed program, otherwise it doesn't and the output is dropped (default) or printed to
        'output_fobjs'.

        If the 'mix_output' argument is 'True', the standard output and error streams will be mixed
        together.

        The 'output_fobjs' is a tuple which may provide 2 file-like objects where the standard
        output and error streams of the executed program should be echoed to. If 'mix_output' is
        'True', the 'output_fobjs[1]' file-like object, which corresponds to the standard error
        stream, will be ignored and all the output will be echoed to 'output_fobjs[0]'. By default
        the command output is not echoed anywhere.

        Note, 'capture_output' and 'output_fobjs' arguments can be used at the same time. It is OK
        to echo the output to some files and capture it at the same time.

        The 'join' argument controls whether the captured output is returned as a single string or a
        list of lines (trailing newline is not stripped).

        The 'cwd' argument may be used to specify the working directory of the command.

        The 'shell' argument controls whether the command should be run via a shell on the remote
        host. Most SSH servers will use user shell to run the command anyway. But there are rare
        cases when this is not the case, and 'shell=False' may be handy.

        The 'intsh' argument indicates whether the command should run in an interactive shell or in
        a separate SSH session. The former is faster because creating a new SSH session takes time.
        By default, 'intsh' is the same as 'shell' ('True' if using shell is allowed, 'False'
        otherwise).

        This function returns an named tuple of (exitcode, stdout, stderr), where
          o 'stdout' is the output of the executed command to stdout
          o 'stderr' is the output of the executed command to stderr
          o 'exitcode' is the integer exit code of the executed command

        If the 'mix_output' argument is 'True', the 'stderr' part of the returned tuple will be an
        empty string.

        If the 'capture_output' argument is not 'True', the 'stdout' and 'stderr' parts of the
        returned tuple will be an empty string.
        """

        msg = f"running the following command{self.hostmsg} (shell {shell}, intsh {intsh}):\n" \
              f"{command}"
        if cwd:
            msg += f"\nWorking directory: {cwd}"
        _LOG.debug(msg)

        if intsh is None:
            intsh = shell

        # Execute the command on the remote host.
        chan = self._run_async(command, cwd=cwd, shell=shell, intsh=intsh)
        if mix_output:
            chan.set_combine_stderr(True)

        # Wait for the command to finish and handle the time-out situation.
        if join:
            by_line = False
        else:
            by_line = True
        result = chan.wait_for_cmd(timeout=timeout, capture_output=capture_output,
                                   output_fobjs=output_fobjs, by_line=by_line, join=join)

        if result.exitcode is None:
            msg = self.cmd_failed_msg(command, *tuple(result), timeout=timeout)
            raise ErrorTimeOut(msg)

        if output_fobjs[0]:
            output_fobjs[0].flush()
        if output_fobjs[1]:
            output_fobjs[1].flush()

        return result

    def run_verify(self, command, timeout=None, capture_output=True, mix_output=False,
                   join=True, output_fobjs=(None, None), cwd=None, shell=True, intsh=None):
        """
        Same as the "run()" method, but also verifies the exit status and if the command failed,
        raises the "Error" exception.
        """

        result = self.run(command, timeout=timeout, capture_output=capture_output,
                          mix_output=mix_output, join=join, output_fobjs=output_fobjs, cwd=cwd,
                          shell=shell, intsh=intsh)
        if result.exitcode == 0:
            return (result.stdout, result.stderr)

        msg = self.cmd_failed_msg(command, *tuple(result), timeout=timeout)
        raise Error(msg)

    def get_ssh_opts(self):
        """
        Returns 'ssh' command-line tool options that are necessary to establish an SSH connection
        similar to the current connection.
        """

        ssh_opts = f"-o \"Port={self.port}\" -o \"User={self.username}\""
        if self.privkeypath:
            ssh_opts += f" -o \"IdentityFile={self.privkeypath}\""
        return ssh_opts

    def rsync(self, src, dst, opts="rlptD", remotesrc=True, remotedst=True):
        """
        Copy data from path 'src' to path 'dst' using 'rsync' with options specified in 'opts'. By
        default the 'src' and 'dst' path is assumed to be on the remote host, but the 'rmotesrc' and
        'remotedst' arguments can be set to 'False' to specify local source and/or destination
        paths.
        """

        cmd = f"rsync -{opts}"
        if remotesrc and remotedst:
            proc = self
        else:
            proc = Procs.Proc()

            cmd += f" -e 'ssh {self.get_ssh_opts()}'"

            if remotesrc:
                src = f"{self.hostname}:{src}"
            if remotedst:
                dst = f"{self.hostname}:{dst}"

        try:
            proc.run_verify(f"{cmd} -- '{src}' '{dst}'")
        except proc.Error as err:
            raise Error(f"failed to copy files '{src}' to '{dst}':\n{err}") from err

    def _scp(self, src, dst):
        """
        Helper that copies 'src' to 'dst' using 'scp'. File names should be already quoted. The
        remote path should use double quoting, otherwise 'scp' fails if path contains symbols like
        ')'.
        """

        opts = f"-o \"Port={self.port}\" -o \"User={self.username}\""
        if self.privkeypath:
            opts += f" -o \"IdentityFile={self.privkeypath}\""
        cmd = f"scp -r {opts}"

        try:
            Procs.run_verify(f"{cmd} -- {src} {dst}")
        except Procs.Error as err:
            raise Error(f"failed to copy files '{src}' to '{dst}':\n{err}") from err

    def get(self, remote_path, local_path):
        """
        Copy a file or directory 'remote_path' from the remote host to 'local_path' on the local
        machine.
        """

        self._scp(f"{self.hostname}:'\"{remote_path}\"'", f"\"{local_path}\"")

    def put(self, local_path, remote_path):
        """
        Copy local file or directory defined by 'local_path' to 'remote_path' on the remote host.
        """

        self._scp(f"\"{local_path}\"", f"{self.hostname}:'\"{remote_path}\"'")

    def cmd_failed_msg(self, command, stdout, stderr, exitcode, startmsg=None, timeout=None):
        """A simple wrapper around '_Common.cmd_failed_msg()'."""

        return _Common.cmd_failed_msg(command, stdout, stderr, exitcode, hostname=self.hostname,
                                      startmsg=startmsg, timeout=timeout)

    def __new__(cls, *_, **kwargs):
        """
        This method makes sure that when users creates an 'SSH' object with 'None' 'hostname', we
        create an instance of 'Proc' class instead of an instance of 'SSH' class. The two classes
        have similar API.
        """

        if "hostname" not in kwargs or kwargs["hostname"] is None:
            return Procs.Proc()
        return super().__new__(cls)

    def _cfg_lookup(self, optname, hostname, username, cfgfiles=None):
        """
        Search for option 'optname' in SSH configuration files. Only consider host 'hostname'
        options.
        """

        old_username = None
        try:
            old_username = os.getenv("USER")
            os.environ["USER"] = username

            if not cfgfiles:
                cfgfiles = []
                for cfgfile in ["/etc/ssh/ssh_config", os.path.expanduser('~/.ssh/config')]:
                    if os.path.exists(cfgfile):
                        cfgfiles.append(cfgfile)

            config = paramiko.SSHConfig()
            for cfgfile in cfgfiles:
                with open(cfgfile, "r") as fobj:
                    config.parse(fobj)

            cfg = config.lookup(hostname)
            if optname in cfg:
                return cfg[optname]
            if "include" in cfg:
                cfgfiles = glob.glob(cfg['include'])
                return self._cfg_lookup(optname, hostname, username, cfgfiles=cfgfiles)
            return None
        finally:
            os.environ["USER"] = old_username

    def _lookup_privkey(self, hostname, username, cfgfiles=None):
        """Lookup for private SSH authentication keys for host 'hostname'."""

        privkeypath = self._cfg_lookup("identityfile", hostname, username, cfgfiles=cfgfiles)
        if isinstance(privkeypath, list):
            privkeypath = privkeypath[0]
        return privkeypath

    def __init__(self, hostname=None, ipaddr=None, port=None, username=None, password="",
                 privkeypath=None, timeout=None):
        """
        Initialize a class instance and establish SSH connection to host 'hostname'. The arguments
        are:
          o hostname - name of the host to connect to
          o ipaddr - optional IP address of the host to connect to. If specified, then it is used
            instead of hostname, otherwise hostname is used.
          o port - optional port number to connect to, default is 22
          o username - optional user name to use when connecting
          o password - optional password to authenticate the 'username' user (not secure!)
          o privkeypath - optional public key path to use for authentication
          o timeout - optional SSH connection timeout value in seconds

        The 'hostname' argument being 'None' is a special case - this module falls-back to using the
        'Procs' module and runs all all operations locally without actually involving SSH or
        networking. This is different to using 'localhost', which does involve SSH.

        SECURITY NOTICE: this class and any part of it should only be used for debugging and
        development purposes. No security audit had been done. Not for production use.
        """

        self.ssh = None
        self.is_remote = True
        self.hostname = hostname
        self.hostmsg = f" on host '{hostname}'"
        self.connection_timeout = timeout
        if port is None:
            port = 22
        self.port = port
        look_for_keys = False
        self.username = username
        self.password = password
        self.privkeypath = privkeypath

        self._sftp = None
        # The interactive shell session.
        self._intsh = None
        # Whether we already run a process in the interactive shell.
        self._intsh_busy = False
        # A lock protecting 'self._intsh_busy' and 'self._intsh'. Basically this lock makes sure we
        # always run exactly one process in the interactive shell.
        self._intsh_lock = threading.Lock()

        if not self.username:
            self.username = os.getenv("USER")
            if not self.username:
                self.username = _get_username()

        if ipaddr:
            connhost = ipaddr
            printhost = f"{hostname} ({ipaddr})"
        else:
            connhost = self._cfg_lookup("hostname", hostname, self.username)
            if connhost:
                printhost = f"{hostname} ({connhost})"
            else:
                printhost = connhost = hostname

        timeoutstr = str(timeout)
        if not self.connection_timeout:
            timeoutstr = "(default)"

        if not self.privkeypath:
            # Try finding the key filename from the SSH configuration files.
            look_for_keys = True
            with contextlib.suppress(Exception):
                self.privkeypath = Path(self._lookup_privkey(hostname, self.username))

        key_filename = str(self.privkeypath) if self.privkeypath else None

        if key_filename:
            # Private SSH key sanity checks.
            try:
                mode = os.stat(key_filename).st_mode
            except OSError:
                raise Error(f"'stat()' failed for private SSH key at '{key_filename}'") from None

            if not stat.S_ISREG(mode):
                raise Error(f"private SSH key at '{key_filename}' is not a regular file")

            if mode & (stat.S_IROTH | stat.S_IWOTH | stat.S_IXOTH):
                raise Error(f"private SSH key at '{key_filename}' permissions are too wide: make "
                            f" sure 'others' cannot read/write/execute it")

        try:
            self.ssh = paramiko.SSHClient()
            self.ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            # We expect to be authenticated either with the key or an empty password.
            self.ssh.connect(username=self.username, hostname=connhost, port=port,
                             key_filename=key_filename, timeout=self.connection_timeout,
                             password=self.password, allow_agent=False, look_for_keys=look_for_keys)
        except paramiko.AuthenticationException as err:
            raise ErrorConnect(f"SSH authentication failed when connecting to {printhost} as "
                               f"'{self.username}':\n{err}") from err
        except Exception as err:
            raise ErrorConnect(f"cannot establish TCP connection to {printhost} with {timeoutstr} "
                               f"secs time-out:\n{err}") from err

        _LOG.debug("established SSH connection to %s, port %d, username '%s', timeout '%s', "
                   "priv. key '%s'", printhost, port, self.username, timeoutstr, self.privkeypath)

    def close(self):
        """Close the SSH connection."""

        if self._sftp:
            sftp = self._sftp
            self._sftp = None
            sftp.close()

        if self._intsh:
            intsh = self._intsh
            self._intsh = None
            with contextlib.suppress(Exception):
                intsh.send("exit\n")
            intsh.close()

        if self.ssh:
            ssh = self.ssh
            self.ssh = None
            ssh.close()

    def _get_sftp(self):
        """Get an SFTP server object."""

        if self._sftp:
            return self._sftp

        try:
            self._sftp = self.ssh.open_sftp()
        except _PARAMIKO_EXCEPTIONS as err:
            raise Error(f"failed to establish SFTP session with {self.hostname}:\n{err}") from err

        return self._sftp

    def open(self, path, mode):
        """
        Open a file on the remote host at 'path' using mode 'mode' (the arguments are the same as in
        the builtin Python 'open()' function).
        """

        def _read_(fobj, size=None):
            """
            SFTP file objects support only binary mode. This wrapper adds basic text mode support.
            """

            try:
                data = fobj._orig_fread_(size=size)
            except BaseException as err:
                raise Error(f"failed to read from '{fobj._orig_fpath_}': {err}") from err

            if "b" not in fobj._orig_fmode_:
                try:
                    data = data.decode("utf8")
                except UnicodeError as err:
                    raise Error(f"failed to decode data read from '{fobj._orig_fpath_}':\n{err}") \
                          from None

            return data

        def _write_(fobj, data):
            """
            SFTP file objects support only binary mode. This wrapper adds basic text mode support.
            """

            if "b" not in fobj._orig_fmode_:
                try:
                    data = data.encode("utf8")
                except UnicodeError as err:
                    raise Error(f"failed to encode data before writing to "
                                f"'{fobj._orig_fpath_}':\n{err}") from None

            errmsg = f"failed to write to '{fobj._orig_fpath_}': "
            try:
                return fobj._orig_fwrite_(data)
            except PermissionError as err:
                raise ErrorPermissionDenied(f"{errmsg}{err}") from None
            except BaseException as err:
                raise Error(f"{errmsg}{err}") from err

        def get_err_prefix(fobj, method):
            """Return the error message prefix."""
            return f"method '{method}()' failed for file '{fobj._orig_fpath_}'"

        path = str(path) # In case it is a pathlib.Path() object.
        sftp = self._get_sftp()

        try:
            fobj = sftp.file(path, mode)
        except _PARAMIKO_EXCEPTIONS as err:
            raise Error(f"failed to open file '{path}' on {self.hostname} via SFTP:\n{err}") \
                  from err

        # Save the path and the mode in the object.
        fobj._orig_fpath_ = path
        fobj._orig_fmode_ = mode

        # Redefine the 'read()' and 'write()' methods to do decoding on the fly, because all files
        # are binary in case of SFTP.
        if "b" not in mode:
            fobj._orig_fread_ = fobj.read
            fobj.read = types.MethodType(_read_, fobj)
            fobj._orig_fwrite_ = fobj.write
            fobj.write = types.MethodType(_write_, fobj)

        # Make sure methods of 'fobj' always raise the 'Error' exception.
        fobj = WrapExceptions.WrapExceptions(fobj, exceptions=_PARAMIKO_EXCEPTIONS,
                                             get_err_prefix=get_err_prefix)
        return fobj

    def __enter__(self):
        """Enter the runtime context."""
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        """Exit the runtime context."""
        self.close()
