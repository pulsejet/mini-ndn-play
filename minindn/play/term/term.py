import os
import logging
import msgpack
import typing
import fcntl
import struct
import termios
import random
import shutil

from contextlib import redirect_stdout, redirect_stderr
from code import InteractiveConsole

from mininet.net import Mininet
from mininet.cli import CLI
from minindn.play.consts import WSKeys, WSFunctions
from minindn.play.socket import PlaySocket
from minindn.play.term.pty import Pty
from minindn.util import getPopen
import minindn.play.util as util

class TermExecutor:
    net: Mininet = None
    pty_list: typing.Dict[str, Pty] = {}
    socket: PlaySocket = None

    def __init__(self, net: Mininet, socket: PlaySocket):
        self.net = net
        self.socket = socket

    def start_cli(self):
        """UI Function: Start CLI"""
        # Send logs to UI
        class WsCliHandler():
            parent: TermExecutor = None

            def __init__(self, parent):
                self.parent = parent

            def write(self, msg: str):
                mb = msg.encode('utf-8')
                self.parent._send_pty_out(mb, "cli")
                self.parent.pty_list["cli"].buffer.write(mb)

        lg = logging.getLogger("mininet")
        handler = logging.StreamHandler(WsCliHandler(self))
        handler.terminator = ""
        lg.addHandler(handler)

        # Create pty for cli
        cpty = Pty(self)
        cpty.id = "cli"
        cpty.name = "MiniNDN CLI"
        cpty.start()

        # Start cli
        CLI.use_rawinput = False
        CLI(self.net, stdin=os.fdopen(cpty.slave, 'r'), stdout=os.fdopen(cpty.slave, 'w'))

    def start_repl(self):
        """UI Function: Start REPL"""

        cpty = Pty(self)
        cpty.id = "repl"
        cpty.name = "Python REPL"
        cpty.start()

        try:
            with os.fdopen(cpty.slave, 'w') as fout, os.fdopen(cpty.slave, 'r') as fin, redirect_stdout(fout), redirect_stderr(fout):
                def raw_input(prompt="") -> str:
                    print(prompt, end="", flush=True)
                    return fin.readline()
                repl = InteractiveConsole({
                    "net": self.net,
                })
                repl.raw_input = raw_input
                repl.interact(None, None)
        except OSError:
            pass

    async def open_all_ptys(self):
        """UI Function: Open all ptys currently active"""
        for key in self.pty_list:
            cpty = self.pty_list[key]
            self.socket.send_all(msgpack.dumps({
                WSKeys.MSG_KEY_FUN: WSFunctions.OPEN_TERMINAL,
                WSKeys.MSG_KEY_RESULT: self._open_term_response(cpty)
            }))

    async def open_term(self, nodeId: str):
        """UI Function: Open new bash terminal"""
        if not util.is_valid_hostid(self.net, nodeId):
            return

        # Copy .bashrc to node
        path = os.path.expanduser("~/.bashrc")
        if os.path.isfile(path):
            # Do this copy every time to make sure the file is up to date
            target = util.host_home(self.net[nodeId]) + "/.bashrc"
            shutil.copy(path, target)

            # Append extra commands
            with open(target, "a") as f:
                # Shell prompt
                f.write("\nexport PS1='\\[\\033[01;32m\\]\\u@{}\\[\\033[00m\\]:\\[\\033[01;34m\]\\w\\[\\033[00m\\]\\$ '\n".format(nodeId))

        # Create pty
        cpty = Pty(self)
        cpty.process = getPopen(
            self.net[nodeId], 'bash --noprofile -i',
            stdin=cpty.slave, stdout=cpty.slave, stderr=cpty.slave)

        cpty.id = nodeId + str(int(random.random() * 100000))
        cpty.name = "bash [{}]".format(nodeId)
        cpty.start()

        return self._open_term_response(cpty)

    async def pty_in(self, id: str, msg: msgpack.ExtType):
        """UI Function: Send input to pty"""
        if id not in self.pty_list:
            return

        if id == "cli" and msg.data == b'\x03':
            # interrupt
            for node in self.net.hosts:
                if node.waiting:
                    node.sendInt()

        self.pty_list[id].stdin.write(msg.data)
        self.pty_list[id].stdin.flush()

    async def pty_resize(self, ptyid, rows, cols):
        """UI Function: Resize pty"""
        if ptyid not in self.pty_list:
            return

        winsize = struct.pack("HHHH", rows, cols, 0, 0)
        fcntl.ioctl(self.pty_list[ptyid].master, termios.TIOCSWINSZ, winsize)

    def _send_pty_out(self, msg: bytes, id: str):
        """Send output to UI"""
        self.socket.send_all(msgpack.dumps({
            WSKeys.MSG_KEY_FUN: WSFunctions.PTY_OUT,
            WSKeys.MSG_KEY_ID: id,
            WSKeys.MSG_KEY_RESULT: msg,
        }))

    def _open_term_response(self, cpty: Pty):
        """Return response for open terminal"""
        return { 'id': cpty.id, 'name': cpty.name, 'buf': cpty.buffer.read() }