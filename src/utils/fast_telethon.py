import os
import math
import asyncio
from telethon import TelegramClient
from telethon.tl.types import InputDocumentFileLocation, InputPhotoFileLocation, InputFileLocation, Document, Photo, MessageMediaDocument, MessageMediaPhoto
import hashlib
import random

# Standard FastTelethon constants
CHUNK_SIZE = 512 * 1024  # 512 KB per chunk
PARALLEL_WORKERS = 4     # Number of parallel upload/download connections
USER_SAFE_DELAY = True   # Add random small delays to avoid User flood bans


async def _download_worker(client: TelegramClient, location, offset: int, chunk_size: int, file_handle):
    """
    Downloads a specific chunk of a file.
    """
    try:
        data = await client.download_file(location, offset=offset, limit=chunk_size)
        file_handle.seek(offset)
        file_handle.write(data)
    except Exception as e:
        print(f"[FastTelethon] Error downloading chunk at offset {offset}: {e}")
        raise e


async def parallel_download(client: TelegramClient, document, file_path: str):
    """
    Downloads a media document in parallel chunks.
    """
    if isinstance(document, MessageMediaDocument) or isinstance(document, MessageMediaPhoto):
        document = document.document if hasattr(document, 'document') else document.photo

    if isinstance(document, Document):
        size = document.size
        location = InputDocumentFileLocation(
            id=document.id,
            access_hash=document.access_hash,
            file_reference=document.file_reference,
            thumb_size=""
        )
    elif isinstance(document, Photo):
        # Find the largest photo size
        largest_size = max(document.sizes, key=lambda s: getattr(s, 'size', 0) if hasattr(s, 'size') else 0)
        size = getattr(largest_size, 'size', 0)
        location = InputPhotoFileLocation(
            id=document.id,
            access_hash=document.access_hash,
            file_reference=document.file_reference,
            thumb_size=largest_size.type
        )
    else:
        # Fallback to standard telethon download if not a standard document/photo
        return await client.download_media(document, file_path)

    print(f"[FastTelethon] Starting parallel download of {size / (1024*1024):.2f} MB to {file_path}")

    with open(file_path, "wb") as f:
        # Pre-allocate file size
        f.seek(size - 1)
        f.write(b"\0")
        f.seek(0)
        
        tasks = []
        for offset in range(0, size, CHUNK_SIZE):
            task = _download_worker(client, location, offset, CHUNK_SIZE, f)
            tasks.append(task)
            
            # Limit parallel workers
            if len(tasks) >= PARALLEL_WORKERS:
                await asyncio.gather(*tasks)
                tasks.clear()
                if USER_SAFE_DELAY:
                    await asyncio.sleep(random.uniform(0.2, 0.6))
                
        # Drain remaining tasks
        if tasks:
            await asyncio.gather(*tasks)

    print(f"[FastTelethon] Download complete.")
    return file_path


async def _upload_worker(client: TelegramClient, file_path: str, file_id: int, part_index: int, chunk_size: int, is_large: bool):
    """
    Uploads a specific chunk of a local file.
    """
    offset = part_index * chunk_size
    with open(file_path, "rb") as f:
        f.seek(offset)
        data = f.read(chunk_size)
    
    try:
        if is_large:
            await client.upload_file(file=data, part_index=part_index, file_name=f"{file_id}.part", use_cache=False)
        else:
             # Standard upload for smaller files logic inside upload_file but doing chunking
            await client.upload_file(file=data, part_index=part_index, file_name=f"{file_id}.part", use_cache=False)
    except Exception as e:
        print(f"[FastTelethon] Error uploading chunk {part_index}: {e}")
        raise e


async def parallel_upload(client: TelegramClient, file_path: str):
    """
    Uploads a local file in parallel chunks and returns an InputFile object.
    """
    size = os.path.getsize(file_path)
    file_id = getattr(client, '_client_id', 0) + int(asyncio.get_event_loop().time())
    
    file_name = os.path.basename(file_path)
    
    print(f"[FastTelethon] Starting parallel upload of {file_name} ({size / (1024*1024):.2f} MB)...")

    part_count = math.ceil(size / CHUNK_SIZE)
    is_large = size > 10 * 1024 * 1024

    # Generate MD5 for small files (Telegram requirement)
    md5_checksum = ""
    if not is_large:
        with open(file_path, "rb") as f:
            md5_checksum = hashlib.md5(f.read()).hexdigest()

    # Create the standard InputFile or InputFileBig
    from telethon.tl.types import InputFile, InputFileBig

    tasks = []
    # Telethon handles the actual mapping of part indices internally through upload_file's raw logic,
    # but since upload_file itself reads the whole file sequentially, we manually invoke save_file_part.
    from telethon.tl.functions.upload import SaveFilePartRequest, SaveBigFilePartRequest
    
    for i in range(part_count):
        offset = i * CHUNK_SIZE
        
        async def upload_part(part_index, off):
            with open(file_path, "rb") as f:
                f.seek(off)
                chunk_data = f.read(CHUNK_SIZE)
            
            if is_large:
                req = SaveBigFilePartRequest(file_id=file_id, file_part=part_index, file_total_parts=part_count, bytes=chunk_data)
            else:
                req = SaveFilePartRequest(file_id=file_id, file_part=part_index, bytes=chunk_data)
                
            tasks.append(client(req))



    if is_large:
        for i in range(part_count):
            offset = i * CHUNK_SIZE
            await upload_part(i, offset)
            
            if len(tasks) >= PARALLEL_WORKERS:
                 await asyncio.gather(*tasks)
                 tasks.clear()
                 if USER_SAFE_DELAY:
                     await asyncio.sleep(random.uniform(0.2, 0.6))
                 
        if tasks:
             await asyncio.gather(*tasks)
    else:
        # For small files under 10MB, the parallel overhead isn't worth it and standard upload is just as fast and immensely safer
        print(f"[FastTelethon] File is small, using standard optimized upload...")
        return await client.upload_file(file=file_path, file_name=file_name)

    print(f"[FastTelethon] Upload complete.")
    
    if is_large:
        return InputFileBig(id=file_id, parts=part_count, name=file_name)
    else:
        return InputFile(id=file_id, parts=part_count, name=file_name, md5_checksum=md5_checksum)
