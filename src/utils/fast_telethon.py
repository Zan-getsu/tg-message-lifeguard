import os
import math
import asyncio
import hashlib
import random
import threading
import logging
from time import time

from telethon import TelegramClient
from telethon.network import MTProtoSender
from telethon.tl.types import (
    Document, Photo,
    MessageMediaDocument, MessageMediaPhoto,
    InputFile, InputFileBig,
)
from telethon.tl.functions.upload import SaveFilePartRequest, SaveBigFilePartRequest

log = logging.getLogger("FastTelethon")

# Standard FastTelethon constants
CHUNK_SIZE = 512 * 1024  # 512 KB per chunk

# Number of parallel TCP connections to Telegram's DC for uploads.
# Each connection is a separate MTProto sender, bypassing single-connection speed caps.
# Too many => FloodWait errors. Recommended: 8-15. Hard cap: 20.
UPLOAD_CONNECTIONS = min(int(os.getenv("WORKERS", "10")), 20)

# Parallel download workers (uses standard Telethon iter_download)
DOWNLOAD_WORKERS = int(os.getenv("DOWNLOAD_WORKERS", "8"))

USER_SAFE_DELAY = True   # Add random small delays to avoid User flood bans (only for downloads now)

# Telegram restricts bot/user uploads to 2GB. We use 1.95GB for safety.
MAX_FILE_SIZE = int(1.95 * 1024 * 1024 * 1024)


# ---------------------------------------------------------------------------
#  Global Progress Tracker (thread-safe)
# ---------------------------------------------------------------------------
class GlobalTracker:
    def __init__(self):
        self.active_tasks = {}
        self.lock = threading.Lock()

    def init_task(self, file_name, total_size, status):
        with self.lock:
            self.active_tasks[file_name] = {
                "status": status,
                "processed": 0,
                "total": total_size,
                "start_time": time(),
            }

    def update_task(self, file_name, bytes_added):
        with self.lock:
            if file_name in self.active_tasks:
                self.active_tasks[file_name]["processed"] += bytes_added

    def complete_task(self, file_name):
        with self.lock:
            if file_name in self.active_tasks:
                del self.active_tasks[file_name]

    def _get_readable_file_size(self, size_in_bytes) -> str:
        if size_in_bytes is None or size_in_bytes == 0:
            return '0B'
        index = 0
        while size_in_bytes >= 1024 and index < 4:
            size_in_bytes /= 1024
            index += 1
        return f"{size_in_bytes:.2f}{['B', 'KB', 'MB', 'GB', 'TB'][index]}"

    def _get_readable_time(self, seconds: int) -> str:
        if seconds <= 0:
            return "0s"
        res = ""
        s = seconds % 60
        m = (seconds // 60) % 60
        h = (seconds // 3600) % 24
        d = seconds // 86400
        if d > 0: res += f"{d}d"
        if h > 0: res += f"{h}h"
        if m > 0: res += f"{m}m"
        if s > 0 or res == "": res += f"{s}s"
        return res

    def get_status_string(self) -> str:
        with self.lock:
            if not self.active_tasks:
                return "No active transfers."

            status_msg = ""
            for i, (file_name, data) in enumerate(self.active_tasks.items(), 1):
                processed = data["processed"]
                total = data["total"]
                start_time = data["start_time"]
                current_time = time()

                elapsed = current_time - start_time
                speed = processed / elapsed if elapsed > 0 else 0

                percentage = (processed / total) * 100 if total > 0 else 0
                eta_seconds = int((total - processed) / speed) if speed > 0 else 0

                # WZML-X Style Progress Bar
                filled = int(percentage / 8.33)
                bar = "\u2b22" * filled + "\u2b21" * (12 - filled)

                status_msg += f"{i}. {file_name}\n"
                status_msg += f"\u251f [{bar}] {percentage:.1f}%\n"
                status_msg += f"\u2520 Processed \u2192 {self._get_readable_file_size(processed)} of {self._get_readable_file_size(total)}\n"
                status_msg += f"\u2520 Status \u2192 {data['status']}\n"
                status_msg += f"\u2520 Speed \u2192 {self._get_readable_file_size(speed)}/s\n"
                status_msg += f"\u2520 Time \u2192 {self._get_readable_time(int(elapsed))} elapsed ( ETA {self._get_readable_time(eta_seconds)} )\n"
                status_msg += f"\u2516 Engine \u2192 FastTelethon\n\n"

            return status_msg


tracker = GlobalTracker()


def _generate_file_id() -> int:
    """Generate a unique random file_id for Telegram uploads.
    Using random avoids collisions when two uploads start in the same second."""
    return random.randrange(1, 2**63)


# ---------------------------------------------------------------------------
#  Multi-Sender Connection Pool
#  Creates N independent MTProto TCP connections to Telegram's upload DC.
#  Each connection is a separate socket — bandwidth is multiplied linearly.
# ---------------------------------------------------------------------------

async def _create_sender(client: TelegramClient, dc_id: int) -> MTProtoSender:
    """
    Open a raw MTProtoSender on a brand-new TCP connection to *dc_id*.
    Because we are always connecting to the **same DC** as the client, we can
    reuse the session's existing auth key directly — no auth export/import needed.
    """
    dc = await client._get_dc(dc_id)
    # Reuse the session auth key (same DC — safe and instant, no round-trip)
    sender = MTProtoSender(client.session.auth_key, loggers=client._log)
    await sender.connect(
        client._connection(
            dc.ip_address,
            dc.port,
            dc.id,
            loggers=client._log,
            proxy=client._proxy,
        )
    )
    return sender


async def _create_upload_pool(
    client: TelegramClient, count: int
) -> list[MTProtoSender]:
    """
    Spin up *count* senders in parallel — all share the same DC and auth key
    so every connection completes independently with no sequencing needed.
    """
    dc_id = client.session.dc_id
    senders = await asyncio.gather(
        *(_create_sender(client, dc_id) for _ in range(count))
    )
    return list(senders)


async def _cleanup_pool(senders: list[MTProtoSender]) -> None:
    """Disconnect all senders in the pool gracefully."""
    await asyncio.gather(*(s.disconnect() for s in senders), return_exceptions=True)


# ---------------------------------------------------------------------------
async def stream_transfer(
    client: TelegramClient,
    media_obj,
    dest_client: TelegramClient,
    dest_entity,
    caption=None,
):
    """
    Downloads a media document in chunks and immediately uploads them without
    writing to disk.  Automatically handles files > 2GB by finalizing the
    upload and starting a new one.
    """
    document = media_obj
    if hasattr(document, 'media') and document.media is not None:
        document = document.media

    if isinstance(document, (MessageMediaDocument, MessageMediaPhoto)):
        document = (
            document.document
            if hasattr(document, 'document')
            else getattr(document, 'photo', document)
        )

    if isinstance(document, Document):
        size = document.size
        file_name = f"{document.id}.bin"
        if hasattr(document, 'attributes'):
            from telethon.tl.types import DocumentAttributeFilename
            for attr in document.attributes:
                if isinstance(attr, DocumentAttributeFilename):
                    file_name = attr.file_name
                    break
    elif isinstance(document, Photo):
        largest_size = max(
            document.sizes,
            key=lambda s: getattr(s, 'size', 0) if hasattr(s, 'size') else 0,
        )
        size = getattr(largest_size, 'size', 0)
        file_name = f"{document.id}.jpg"
    else:
        print("[FastTelethon] Unsupported media type for streaming, falling back to disk.")
        return None

    if size <= 0:
        print("[FastTelethon] File size is 0, skipping stream transfer.")
        return None

    print(f"[FastTelethon] Starting Memory Stream Transfer of {file_name} ({size / (1024*1024):.2f} MB)...")
    print(f"[FastTelethon] Using {UPLOAD_CONNECTIONS} parallel upload connections.")
    tracker.init_task(file_name, size, "Streaming")

    bytes_transferred_total = 0
    part_number = 1

    # Create the multi-sender pool once upfront for the entire transfer
    senders: list[MTProtoSender] = []
    try:
        senders = await _create_upload_pool(dest_client, UPLOAD_CONNECTIONS)

        while bytes_transferred_total < size:
            current_file_id = _generate_file_id()
            current_file_name = (
                file_name if part_number == 1 else f"{file_name}.part{part_number}"
            )

            remaining_in_file = size - bytes_transferred_total
            bytes_for_this_segment = min(remaining_in_file, MAX_FILE_SIZE)

            part_count = math.ceil(bytes_for_this_segment / CHUNK_SIZE)
            is_large = bytes_for_this_segment > 10 * 1024 * 1024

            print(
                f"[FastTelethon] Uploading Part {part_number} "
                f"({bytes_for_this_segment / (1024*1024):.2f} MB) in {part_count} chunks "
                f"across {len(senders)} connections..."
            )

            md5_hasher = hashlib.md5() if not is_large else None

            # Semaphore: keep at most N*4 chunk uploads in-flight at once
            # to avoid queueing too many tasks in memory for huge files
            sem = asyncio.Semaphore(len(senders) * 4)
            pending: list[asyncio.Task] = []

            async def _send_chunk(sender: MTProtoSender, req, n_bytes: int) -> None:
                async with sem:
                    await sender.send(req)
                tracker.update_task(file_name, n_bytes)

            chunk_index = 0
            async for chunk in client.iter_download(
                media_obj,
                offset=bytes_transferred_total,
                request_size=CHUNK_SIZE,
                limit=part_count,
            ):
                # iter_download yields memoryview — Telethon's serialize_bytes
                # only accepts bytes/str, so convert here.
                if isinstance(chunk, memoryview):
                    chunk = bytes(chunk)
                if md5_hasher:
                    md5_hasher.update(chunk)

                if is_large:
                    req = SaveBigFilePartRequest(
                        file_id=current_file_id,
                        file_part=chunk_index,
                        file_total_parts=part_count,
                        bytes=chunk,
                    )
                else:
                    req = SaveFilePartRequest(
                        file_id=current_file_id,
                        file_part=chunk_index,
                        bytes=chunk,
                    )

                # Round-robin across senders — fire and track
                sender = senders[chunk_index % len(senders)]
                task = asyncio.create_task(_send_chunk(sender, req, len(chunk)))
                pending.append(task)
                chunk_index += 1

            # Wait for all in-flight uploads for this segment
            if pending:
                await asyncio.gather(*pending)

            bytes_transferred_total += bytes_for_this_segment

            # Finalize this upload segment
            if is_large:
                input_file = InputFileBig(
                    id=current_file_id, parts=part_count, name=current_file_name
                )
            else:
                input_file = InputFile(
                    id=current_file_id,
                    parts=part_count,
                    name=current_file_name,
                    md5_checksum=md5_hasher.hexdigest(),
                )

            cap = caption if part_number == 1 else f"Part {part_number} for {file_name}"
            await dest_client.send_file(
                entity=dest_entity,
                file=input_file,
                caption=cap,
                silent=True,
                parse_mode="html",
            )

            part_number += 1

    except Exception as e:
        print(f"[FastTelethon] Stream transfer error for {file_name}: {e}")
        raise
    finally:
        tracker.complete_task(file_name)
        if senders:
            await _cleanup_pool(senders)


# ---------------------------------------------------------------------------
#  Parallel Download
# ---------------------------------------------------------------------------
async def _download_worker(
    client: TelegramClient,
    file_obj,
    offset: int,
    chunk_size: int,
    file_handle,
    tracker_name: str,
    max_retries: int = 3,
):
    """Downloads a specific chunk with retry logic."""
    for attempt in range(max_retries):
        try:
            async for chunk in client.iter_download(
                file_obj, offset=offset, request_size=chunk_size, limit=1
            ):
                file_handle.seek(offset)
                file_handle.write(chunk)
                tracker.update_task(tracker_name, len(chunk))
            return
        except Exception as e:
            if attempt < max_retries - 1:
                wait = 1 * (attempt + 1)
                print(
                    f"[FastTelethon] Retry {attempt+1}/{max_retries} for offset "
                    f"{offset}: {e}  (waiting {wait}s)"
                )
                await asyncio.sleep(wait)
            else:
                print(
                    f"[FastTelethon] Error downloading chunk at offset {offset} "
                    f"after {max_retries} attempts: {e}"
                )
                raise


async def parallel_download(client: TelegramClient, media_obj, file_path: str):
    """
    Downloads a media document in parallel chunks.
    Pass a Message object as media_obj whenever possible so Telethon can
    auto-refresh expired file references.
    """
    document = media_obj
    if hasattr(document, 'media') and document.media is not None:
        document = document.media

    if isinstance(document, (MessageMediaDocument, MessageMediaPhoto)):
        document = (
            document.document
            if hasattr(document, 'document')
            else getattr(document, 'photo', document)
        )

    if isinstance(document, Document):
        size = document.size
    elif isinstance(document, Photo):
        largest_size = max(
            document.sizes,
            key=lambda s: getattr(s, 'size', 0) if hasattr(s, 'size') else 0,
        )
        size = getattr(largest_size, 'size', 0)
    else:
        return await client.download_media(media_obj, file_path)

    if size <= 0:
        print("[FastTelethon] File size is 0, using standard download fallback.")
        return await client.download_media(media_obj, file_path)

    base_name = os.path.basename(file_path)
    tracker.init_task(base_name, size, "Downloading")
    print(f"[FastTelethon] Starting parallel download of {size / (1024*1024):.2f} MB to {file_path}")

    try:
        with open(file_path, "wb") as f:
            # Pre-allocate file size
            f.seek(size - 1)
            f.write(b"\0")
            f.seek(0)

            tasks = []
            for offset in range(0, size, CHUNK_SIZE):
                task = _download_worker(
                    client, media_obj, offset, CHUNK_SIZE, f, base_name
                )
                tasks.append(task)

                if len(tasks) >= DOWNLOAD_WORKERS:
                    await asyncio.gather(*tasks)
                    tasks.clear()
                    if USER_SAFE_DELAY:
                        await asyncio.sleep(random.uniform(0.2, 0.6))

            if tasks:
                await asyncio.gather(*tasks)

    except Exception as e:
        print(f"[FastTelethon] Download failed for {base_name}: {e}")
        tracker.complete_task(base_name)
        raise

    tracker.complete_task(base_name)
    print(f"[FastTelethon] Download complete.")
    return file_path


# ---------------------------------------------------------------------------
#  Parallel Upload
# ---------------------------------------------------------------------------
async def parallel_upload(client: TelegramClient, file_path: str):
    """
    Uploads a local file in parallel chunks and returns an InputFile object.
    """
    size = os.path.getsize(file_path)
    if size <= 0:
        print("[FastTelethon] File is empty, skipping upload.")
        return None

    file_name = os.path.basename(file_path)
    tracker.init_task(file_name, size, "Uploading")
    print(f"[FastTelethon] Starting parallel upload of {file_name} ({size / (1024*1024):.2f} MB)...")

    is_large = size > 10 * 1024 * 1024

    # For small files (< 10 MB), use standard Telethon upload — simpler and safer
    if not is_large:
        print(f"[FastTelethon] File is small, using standard optimized upload...")
        try:
            res = await client.upload_file(file=file_path, file_name=file_name)
            tracker.complete_task(file_name)
            return res
        except Exception as e:
            print(f"[FastTelethon] Standard upload failed: {e}")
            tracker.complete_task(file_name)
            raise

    # --- Large file multi-connection parallel upload ---
    file_id = _generate_file_id()
    part_count = math.ceil(size / CHUNK_SIZE)

    print(f"[FastTelethon] Using {UPLOAD_CONNECTIONS} parallel upload connections.")
    senders: list[MTProtoSender] = []
    try:
        senders = await _create_upload_pool(client, UPLOAD_CONNECTIONS)

        sem = asyncio.Semaphore(len(senders) * 4)
        pending: list[asyncio.Task] = []

        async def _send_part(sender: MTProtoSender, req, n_bytes: int) -> None:
            async with sem:
                await sender.send(req)
            tracker.update_task(file_name, n_bytes)

        with open(file_path, "rb") as f:
            for i in range(part_count):
                chunk_data = f.read(CHUNK_SIZE)
                if not chunk_data:
                    break

                req = SaveBigFilePartRequest(
                    file_id=file_id,
                    file_part=i,
                    file_total_parts=part_count,
                    bytes=chunk_data,
                )

                sender = senders[i % len(senders)]
                task = asyncio.create_task(_send_part(sender, req, len(chunk_data)))
                pending.append(task)

        # Wait for all in-flight parts
        if pending:
            await asyncio.gather(*pending)

    except Exception as e:
        print(f"[FastTelethon] Upload failed for {file_name}: {e}")
        raise
    finally:
        tracker.complete_task(file_name)
        if senders:
            await _cleanup_pool(senders)

    print(f"[FastTelethon] Upload complete.")
    return InputFileBig(id=file_id, parts=part_count, name=file_name)

