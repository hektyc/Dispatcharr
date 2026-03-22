"""Redis-based segment storage for HLS output.

FFmpeg writes to a staging directory on disk. The watcher pushes
segments to Redis with TTL, then deletes the staged files.
Views serve segments directly from Redis.
"""

import logging
from typing import Optional

from .base import SegmentStore

logger = logging.getLogger(__name__)

# Redis key prefixes for HLS output
PLAYLIST_KEY_PREFIX = "hls_output:{uuid}:playlist"
SEGMENT_KEY_PREFIX = "hls_output:{uuid}:seg:{name}"
SEGMENT_INDEX_KEY = "hls_output:{uuid}:segments"


class RedisSegmentStore(SegmentStore):
    """Redis-backed storage for HLS segments with automatic TTL expiry.

    Segments are stored in Redis with a configurable TTL.
    The watcher monitors the filesystem staging directory,
    pushes new segments/playlists to Redis, then cleans up disk files.
    """

    def __init__(self, redis_client, ttl: int = 120):
        """Initialize Redis segment store.

        Args:
            redis_client: A Redis client instance.
            ttl: Time-to-live for segments in seconds.
        """
        self._redis = redis_client
        self._ttl = ttl

    def _playlist_key(self, channel_uuid: str) -> str:
        return PLAYLIST_KEY_PREFIX.format(uuid=channel_uuid)

    def _segment_key(self, channel_uuid: str, name: str) -> str:
        return SEGMENT_KEY_PREFIX.format(uuid=channel_uuid, name=name)

    def _index_key(self, channel_uuid: str) -> str:
        return SEGMENT_INDEX_KEY.format(uuid=channel_uuid)

    def store_playlist(self, channel_uuid: str, content: str) -> bool:
        """Store playlist in Redis with TTL."""
        try:
            key = self._playlist_key(channel_uuid)
            self._redis.setex(key, self._ttl * 2, content)
            return True
        except Exception as e:
            logger.error("Redis: Failed to store playlist for %s: %s", channel_uuid, e)
            return False

    def get_playlist(self, channel_uuid: str) -> Optional[str]:
        """Retrieve playlist from Redis."""
        try:
            key = self._playlist_key(channel_uuid)
            data = self._redis.get(key)
            if data is None:
                return None
            return data.decode("utf-8") if isinstance(data, bytes) else data
        except Exception as e:
            logger.error("Redis: Failed to get playlist for %s: %s", channel_uuid, e)
            return None

    def store_segment(self, channel_uuid: str, name: str, data: bytes) -> bool:
        """Store segment in Redis with TTL."""
        try:
            key = self._segment_key(channel_uuid, name)
            self._redis.setex(key, self._ttl, data)
            # Track segment in index set
            index_key = self._index_key(channel_uuid)
            self._redis.sadd(index_key, name)
            self._redis.expire(index_key, self._ttl * 2)
            return True
        except Exception as e:
            logger.error(
                "Redis: Failed to store segment %s for %s: %s",
                name, channel_uuid, e
            )
            return False

    def get_segment(self, channel_uuid: str, name: str) -> Optional[bytes]:
        """Retrieve segment from Redis."""
        try:
            key = self._segment_key(channel_uuid, name)
            data = self._redis.get(key)
            return data
        except Exception as e:
            logger.error(
                "Redis: Failed to get segment %s for %s: %s",
                name, channel_uuid, e
            )
            return None

    def get_segment_count(self, channel_uuid: str) -> int:
        """Count segments stored in Redis for a channel."""
        try:
            index_key = self._index_key(channel_uuid)
            return self._redis.scard(index_key) or 0
        except Exception as e:
            logger.error("Redis: Failed to count segments for %s: %s", channel_uuid, e)
            return 0

    def cleanup_channel(self, channel_uuid: str):
        """Remove all Redis keys for a channel."""
        try:
            # Delete playlist
            self._redis.delete(self._playlist_key(channel_uuid))

            # Delete all segments
            index_key = self._index_key(channel_uuid)
            segment_names = self._redis.smembers(index_key)
            if segment_names:
                keys_to_delete = [
                    self._segment_key(
                        channel_uuid,
                        name.decode("utf-8") if isinstance(name, bytes) else name,
                    )
                    for name in segment_names
                ]
                if keys_to_delete:
                    self._redis.delete(*keys_to_delete)

            # Delete index
            self._redis.delete(index_key)
            logger.info("Redis: Cleaned up HLS output for channel %s", channel_uuid)
        except Exception as e:
            logger.error("Redis: Failed to cleanup channel %s: %s", channel_uuid, e)
