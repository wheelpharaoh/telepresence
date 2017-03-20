# Original version copyright (c) Twisted Matrix Laboratories.
# See LICENSE for details.
"""
Implementation of the SOCKSv5 protocol.

In additional to standard SOCKSv5 this also implements the Tor SOCKS protocol
extension for DNS lookups.

References:

https://www.ietf.org/rfc/rfc1928.txt
https://github.com/dgoulet/torsocks/blob/master/doc/socks/socks-extensions.txt
for RESOLVE extension.
"""

# python imports
import socket
import struct

# twisted imports
from twisted.internet import reactor, protocol, defer
from twisted.python import log
from twisted.protocols.stateful import StatefulProtocol


class SOCKSv5Outgoing(protocol.Protocol):
    def __init__(self, socks):
        self.socks = socks

    def connectionMade(self):
        # First thing, make sure SOCKS connection knows about us, so events get
        # handed to us:
        self.socks.otherConn = self
        # Next, tell SOCKS client it can now proceed to send data via the
        # server to this connection. Per the RFC, we return the bind host and
        # port.
        host = self.transport.getHost()
        self.socks._write_response(0, host.host, host.port)

    def connectionLost(self, reason):
        self.socks.transport.loseConnection()

    def dataReceived(self, data):
        self.socks.write(data)

    def write(self, data):
        self.transport.write(data)


class Request(object):
    """A SOCKSv5 request."""

    def __init__(self, command, host, port):
        self.command = command
        self.host = host
        self.port = port


class SOCKSv5(StatefulProtocol):
    """
    An implementation of the SOCKSv5 protocol.

    @type logging: L{str} or L{None}
    @ivar logging: If not L{None}, the name of the logfile to which connection
        information will be written.

    @type reactor: object providing L{twisted.internet.interfaces.IReactorTCP}
    @ivar reactor: The reactor used to create connections.

    @type buf: L{str}
    @ivar buf: Part of a SOCKSv5 connection request.

    @type otherConn: C{SOCKSv5Incoming}, C{SOCKSv5Outgoing} or L{None}
    @ivar otherConn: Until the connection has been established, C{otherConn} is
        L{None}. After that, it is the proxy-to-destination protocol instance
        along which the client's connection is being forwarded.
    """

    def __init__(self, logging=None, reactor=reactor):
        self.logging = logging
        self.reactor = reactor

    def connectionMade(self):
        self.otherConn = None
        self.command = None

    def dataReceived(self, data):
        """
        Called whenever data is received.

        @type data: L{bytes}
        @param data: Part or all of a SOCKSv5 packet.
        """
        print("RECEIVED:", repr(data))
        if self.otherConn is not None:
            # We're in proxying mode now:
            self.otherConn.write(data)
            return
        StatefulProtocol.dataReceived(self, data)

    def getInitialState(self):
        """Starting point for parsing state machine."""
        return self._parse_handshake_start, 2

    def _parse_handshake_start(self, data):
        assert data[0] == 5
        length = data[1]
        return self._parse_handshake_auth, length

    def _parse_handshake_auth(self, data):
        # NO_AUTH response
        self.write(b"\x05\x00")
        return self._parse_request_start, 4

    def _parse_request_start(self, data):
        assert data[0] == 5
        assert data[2] == 0
        command = data[1]
        addr_type = data[3]
        if command == 1:
            self.command = "CONNECT"
        elif command == 240:  # \xF0
            self.command = "RESOLVE"
        else:
            # Unsupported command response
            self._write_response(7, "0.0.0.0", 0)
            return

        if addr_type == 1:
            return self._parse_request_ipv4, 6
        if addr_type == 3:
            return self._parse_request_domainname_start, 1
        else:
            # XXX IPv6 currently unsupported
            self._write_response(7, "0.0.0.0", 0)

    def _parse_request_ipv4(self, data):
        host = socket.inet_ntoa(data[:4])
        port = struct.unpack("!H", data[4:6])[0]
        self._done_parsing(host, port)

    def _parse_request_domainname_start(self, data):
        length = data[0]
        return self._parse_request_domainname, length + 2

    def _parse_request_domainname(self, data):
        host = data[:-2]
        port = struct.unpack("!H", data[-2:])[0]
        self._done_parsing(host, port)

    def _handle_error(self, error):
        """Handle errors in connecting or resolving."""
        log.err(error)
        self._write_response(1, "0.0.0.0", 0)

    def _write_response(self, code, host, port):
        self.write(
            struct.pack("!BBBB", 5, code, 0, 1) +
            socket.inet_aton(host) +
            struct.pack("!H", port))
        if code != 0:
            self.transport.loseConnection()

    def _done_parsing(self, host, port):
        host = str(host, "utf-8")
        if self.command == "CONNECT":
            d = self.connectClass(str(host), port, SOCKSv5Outgoing, self)
            d.addErrback(self._handle_error)
        elif self.command == "RESOLVE":
            def write_response(addr):
                self.write(b"\5\0\0\1" + socket.inet_aton(addr))
                self.transport.loseConnection()

            self.reactor.lookupAddress(
                host,
            ).addCallback(write_response).addErrback(self._handle_error)

    def connectionLost(self, reason):
        if self.otherConn:
            self.otherConn.transport.loseConnection()

    def connectClass(self, host, port, klass, *args):
        return protocol.ClientCreator(reactor, klass, *args
                                      ).connectTCP(host, port)

    def listenClass(self, port, klass, *args):
        serv = reactor.listenTCP(port, klass(*args))
        return defer.succeed(serv.getHost()[1:])

    def write(self, data):
        print("SENT:", repr(data))
        self.transport.write(data)


class SOCKSv5Factory(protocol.Factory):
    """
    A factory for a SOCKSv5 proxy.

    Constructor accepts one argument, a log file name.
    """

    def buildProtocol(self, addr):
        return SOCKSv5(reactor)


if __name__ == '__main__':
    from twisted.python.failure import startDebugMode
    startDebugMode()
    reactor.listenTCP(9050, SOCKSv5Factory())
    reactor.run()
