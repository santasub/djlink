import unittest
from unittest.mock import Mock, patch
import socket

from prodj.network.nfsclient import NfsClient
from prodj.network.packets_nfs import RpcMsg

class MockSock(Mock):
    def __init__(self, inet, type):
        assert inet == socket.AF_INET
        assert type == socket.SOCK_DGRAM
        self.sent = list()

    def sendto(self, data, host):
        msg = RpcMsg.parse(data)
        self.sent += msg
        print(msg)

class DbclientTestCase(unittest.TestCase):
    def setUp(self):
        self.nc = NfsClient(None) # prodj object only required for enqueue_download_from_mount_info
        # TODO: use unittest.mock for replacing socket module
        # self.sock = MockSock
        # NfsClient.socket.socket = self.sock
        self.nc.start() # Start the NfsClient and its event loop

        # assert self.sock.binto.called

    def tearDown(self):
        self.nc.stop() # Stop the NfsClient and its event loop

from unittest.mock import AsyncMock # Add AsyncMock

    @patch('socket.socket', new_callable=Mock) # Keep socket mocked to prevent real network calls if any part of setup tries
    def test_buffer_download(self, mock_socket_fn): # mock_socket_fn is the mocked socket constructor
        # Mock the asynchronous helper methods of NfsClient instance
        self.nc.PortmapGetPort = AsyncMock(side_effect=[111, 2049])  # Mount port, NFS port
        self.nc.MountMnt = AsyncMock(return_value=b"mock_mount_fhandle")

        attrs_mock = Mock()
        attrs_mock.size = 500  # Example size for the mock file
        self.nc.NfsLookupPath = AsyncMock(return_value=Mock(fhandle=b"mock_file_fhandle", attrs=attrs_mock))

        # Prepare the full content that NfsReadData should simulate returning in chunks
        nfs_read_data_full_content = b"test_data_chunk_" * (500 // 16) + b"end" # ensure 500 bytes
        nfs_read_data_full_content = nfs_read_data_full_content[:500]

        async def mock_nfs_read_data_side_effect(host, fhandle, offset, size_requested):
            # Simulate reading chunks of the data
            end_offset = offset + size_requested
            chunk_data = nfs_read_data_full_content[offset:end_offset]
            reply_mock = Mock()
            reply_mock.data = chunk_data
            # Simulate a small delay, otherwise the loop might be too fast for thread switching
            # await asyncio.sleep(0.001)
            return reply_mock

        self.nc.NfsReadData = AsyncMock(side_effect=mock_nfs_read_data_side_effect)

        # Now, call the method under test
        result_buffer = self.nc.enqueue_buffer_download("1.1.1.1", "usb", "/folder/file")

        # Assert that the returned buffer is what we expect
        self.assertEqual(result_buffer, nfs_read_data_full_content)

        # Verify calls (optional, but good for ensuring mocks were used as expected)
        self.nc.PortmapGetPort.assert_any_call("1.1.1.1", "mount", 1, "udp")
        self.nc.PortmapGetPort.assert_any_call("1.1.1.1", "nfs", 3, "udp")
        self.nc.MountMnt.assert_called_once()
        self.nc.NfsLookupPath.assert_called_once()
        self.assertTrue(self.nc.NfsReadData.call_count > 0)
