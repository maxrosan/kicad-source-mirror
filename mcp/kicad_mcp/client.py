"""Minimal client for the KiCad IPC API (protobuf messages over an nng REQ/REP socket)."""

import os
import sys
from pathlib import Path

_PROTO_DIR = Path(__file__).resolve().parent / "_proto"

if not _PROTO_DIR.exists():
    raise ImportError(f"Protobuf bindings not found in {_PROTO_DIR}; run mcp/gen_proto.py first")

# The generated modules import each other as top-level packages (common, schematic, board).
if str(_PROTO_DIR) not in sys.path:
    sys.path.insert(0, str(_PROTO_DIR))

import pynng  # noqa: E402
from google.protobuf.message import Message  # noqa: E402

from common import envelope_pb2  # noqa: E402


class ApiError(RuntimeError):
    """Raised when KiCad answers a request with a status other than AS_OK."""

    def __init__(self, status: int, message: str):
        self.status = status
        name = envelope_pb2.ApiStatusCode.Name(status)
        super().__init__(f"{name}: {message}" if message else name)


class KiCadClient:
    """Sends API commands to one KiCad instance (GUI or `kicad-cli api-server`).

    Args:
        socket_path: filesystem path of the API socket (on Windows nng maps it to a named pipe).
        client_name: identifies this client to KiCad; commits are grouped per client name.
        timeout_ms: how long to wait for each reply.
    """

    def __init__(self, socket_path: str, client_name: str = "kicad-mcp", timeout_ms: int = 20000):
        self.socket_path = socket_path
        self.client_name = client_name
        self.token = ""
        self._socket = pynng.Req0(recv_timeout=timeout_ms, send_timeout=timeout_ms)
        self._socket.dial(f"ipc://{socket_path}", block=True)

    def close(self) -> None:
        self._socket.close()

    def send(self, command: Message, response_type: type[Message]) -> Message:
        """Send one command and unpack the reply into a new `response_type` message."""
        request = envelope_pb2.ApiRequest()
        request.header.kicad_token = self.token
        request.header.client_name = self.client_name
        request.message.Pack(command)

        self._socket.send(request.SerializeToString())

        reply = envelope_pb2.ApiResponse()
        reply.ParseFromString(self._socket.recv())

        if reply.status.status != envelope_pb2.AS_OK:
            raise ApiError(reply.status.status, reply.status.error_message)

        if not self.token:
            self.token = reply.header.kicad_token

        response = response_type()

        if not reply.message.Unpack(response):
            raise ApiError(envelope_pb2.AS_INTERNAL_ERROR,
                           f"expected {response_type.DESCRIPTOR.full_name}, "
                           f"got {reply.message.TypeName()}")

        return response


def default_socket_path() -> str:
    """Socket used by the KiCad GUI when its API server is enabled."""
    return str(Path(os.environ.get("TEMP", "/tmp")) / "kicad" / "api.sock")
