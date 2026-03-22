"""Abstract base class for HLS segment storage backends."""

from abc import ABC, abstractmethod
from typing import Optional


class SegmentStore(ABC):
    """Abstract interface for storing and retrieving HLS segments and playlists."""

    @abstractmethod
    def store_playlist(self, channel_uuid: str, content: str) -> bool:
        """Store/update the playlist for a channel.

        Args:
            channel_uuid: The channel identifier.
            content: The M3U8 playlist content.

        Returns:
            True if stored successfully.
        """
        ...

    @abstractmethod
    def get_playlist(self, channel_uuid: str) -> Optional[str]:
        """Retrieve the playlist for a channel.

        Args:
            channel_uuid: The channel identifier.

        Returns:
            The playlist content, or None if not found.
        """
        ...

    @abstractmethod
    def store_segment(self, channel_uuid: str, name: str, data: bytes) -> bool:
        """Store a segment file.

        Args:
            channel_uuid: The channel identifier.
            name: The segment filename (e.g., 'index0.ts').
            data: The raw segment data.

        Returns:
            True if stored successfully.
        """
        ...

    @abstractmethod
    def get_segment(self, channel_uuid: str, name: str) -> Optional[bytes]:
        """Retrieve a segment file.

        Args:
            channel_uuid: The channel identifier.
            name: The segment filename.

        Returns:
            The raw segment data, or None if not found.
        """
        ...

    @abstractmethod
    def cleanup_channel(self, channel_uuid: str):
        """Clean up all data for a channel.

        Args:
            channel_uuid: The channel identifier.
        """
        ...

    def get_segment_count(self, channel_uuid: str) -> int:
        """Get the count of segments for a channel. Default implementation returns 0."""
        return 0
