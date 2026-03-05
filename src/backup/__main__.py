"""
This module handles the export of deleted Telegram messages and media using Telethon,
with an optional feature to simultaneously re-upload them to a destination group.
"""

import os
import asyncio
import argparse
import html
import json
import glob
from datetime import datetime
from dotenv import load_dotenv
from telethon.sync import TelegramClient, events
from telethon.tl.types import PeerChannel, DocumentAttributeFilename
from telethon.errors import RPCError
from telethon import utils

from src.utils.fast_telethon import parallel_download, parallel_upload, stream_transfer, tracker

load_dotenv()

# Requesting user credentials if not in .env
api_id_env = os.getenv("API_ID")
api_hash_env = os.getenv("API_HASH")

source_group_env = os.getenv("SOURCE_GROUP_ID")
dest_group_env = os.getenv("DESTINATION_GROUP_ID")

api_id: int = int(api_id_env) if api_id_env else int(input("Enter your API_ID: "))
api_hash: str = api_hash_env if api_hash_env else input("Enter your API_HASH: ")

session_name: str = "tg_session"

# Set the output folder
output_folder: str = "backup_will_be_inside_me"
os.makedirs(output_folder, exist_ok=True)

# Telegram clients will be initialized in main()

# Telegram restricts bot/user uploads to 2GB. We use 1.95GB for safety.
MAX_FILE_SIZE = int(1.95 * 1024 * 1024 * 1024)


def _parse_size(size_str: str) -> int:
    """
    Parse a human-readable size string (e.g., '100MB', '1GB', '500KB') into bytes.
    Returns the size in bytes as an integer.
    """
    if not size_str:
        return 0

    size_str = size_str.strip().upper()

    multipliers = {
        'B': 1,
        'KB': 1024,
        'MB': 1024 ** 2,
        'GB': 1024 ** 3,
        'TB': 1024 ** 4,
    }

    for suffix, multiplier in sorted(multipliers.items(), key=lambda x: -len(x[0])):
        if size_str.endswith(suffix):
            try:
                number = float(size_str[:-len(suffix)].strip())
                return int(number * multiplier)
            except ValueError:
                raise ValueError(f"Invalid size format: {size_str}")

    # If no suffix, assume bytes
    try:
        return int(size_str)
    except ValueError:
        raise ValueError(f"Invalid size format: {size_str}") 


def _build_caption(msg_obj: dict) -> str | None:
    """
    Build an HTML-formatted caption string from a message dict.
    Returns None if there is no meaningful text content.
    """
    message_text = msg_obj.get("message", "")
    has_message = bool(message_text.strip())

    if has_message:
        message_text = html.escape(message_text)

    # Handle quoted replies
    reply_to = msg_obj.get("reply_to")
    has_quote = False
    quote_text = None
    if reply_to:
        has_quote = reply_to.get("quote", False)
        quote_text = reply_to.get("quote_text", None)

    if has_quote and quote_text:
        quote_text_escaped = html.escape(quote_text)
        if has_message:
            message_text = f"<pre>❝ {quote_text_escaped} ❞</pre>\n\n{message_text}"
        else:
            message_text = f"<pre>❝ {quote_text_escaped} ❞</pre>"
        has_message = True

    return message_text if has_message else None


async def split_file_if_needed(filepath: str) -> list[str]:
    """
    Checks if a file exceeds the 1.95GB limit and splits it.
    Uses an asynchronous chunked read buffer to prevent massive RAM freezes.
    Returns a list of file paths (either the original file, or the newly split parts).
    """
    if not os.path.exists(filepath):
        return []
    
    file_size = os.path.getsize(filepath)
    if file_size <= MAX_FILE_SIZE:
        return [filepath]
    
    base_name = os.path.basename(filepath)
    tracker.init_task(base_name, file_size, "Splitting")
    print(f"\n[Splitter] File {filepath} is {(file_size / (1024**3)):.2f}GB. Splitting into {MAX_FILE_SIZE / (1024**3):.2f}GB parts...")
    
    part_files = []
    part_num = 1
    
    # 16MB read chunks to keep RAM usage microscopic while splitting
    read_chunk_size = 1024 * 1024 * 16 
    
    with open(filepath, 'rb') as f_in:
        while True:
            part_filename = f"{filepath}.part{part_num}"
            bytes_written_to_part = 0
            
            with open(part_filename, 'wb') as f_out:
                while bytes_written_to_part < MAX_FILE_SIZE:
                    # Read only enough bytes to finish the part or a standard chunk
                    bytes_to_read = min(read_chunk_size, MAX_FILE_SIZE - bytes_written_to_part)
                    chunk = f_in.read(bytes_to_read)
                    
                    if not chunk:
                        break # EOF Reached
                        
                    f_out.write(chunk)
                    bytes_written_to_part += len(chunk)
                    tracker.update_task(base_name, len(chunk))
                    
                    # Yield to the asyncio event loop so we don't block Telegram downloads
                    await asyncio.sleep(0) 

            # Check if we actually wrote anything to this part
            if bytes_written_to_part == 0:
                os.remove(part_filename)
                break
                
            part_files.append(part_filename)
            print(f"[Splitter] Created chunk: {part_filename}")
            part_num += 1
            
            if bytes_written_to_part < MAX_FILE_SIZE:
                 break # EOF Reached naturally during this part
            
    # Remove the huge original file since it's now cleanly split.
    os.remove(filepath)
    tracker.complete_task(base_name)
    print(f"[Splitter] Deleted original oversized file to preserve disk space.")
    
    return part_files


async def uploader_worker(upload_client: TelegramClient, queue: asyncio.Queue, dest_group_id: int):
    """
    Consumer task that listens for completely downloaded files and uploads them.
    """
    if not dest_group_id:
        # If there's no destination, just consume and ignore.
        while True:
            item = await queue.get()
            if item is None: break 
            queue.task_done()
        return

    dest_entity = await upload_client.get_entity(PeerChannel(dest_group_id))

    # To track what we've already uploaded so we don't duplicate on restarts
    dump_file = os.path.join(output_folder, "dump_unified.json")

    while True:
        item = await queue.get()
        if item is None:  # Shutdown signal
            queue.task_done()
            break
            
        event_json_str, file_paths = item
        
        try:
            msg_obj = json.loads(event_json_str)
            
            # --- Format the text message precisely like resender ---
            peer_id = msg_obj["peer_id"].get("channel_id", None) if "peer_id" in msg_obj else None
            
            reply_to_msg_id = None
            reply_to_top_id = None
            quote_text = None
            has_quote = False
            
            if "reply_to" in msg_obj and msg_obj["reply_to"]:
                reply_to_msg_id = msg_obj["reply_to"].get("reply_to_msg_id", None)
                reply_to_top_id = msg_obj["reply_to"].get("reply_to_top_id", None)
                quote_text = msg_obj["reply_to"].get("quote_text", None)
                has_quote = msg_obj["reply_to"].get("quote", False)
                
            reply_to = "/".join(
                str(s).strip()
                for s in [peer_id, reply_to_top_id, reply_to_msg_id]
                if s and str(s).strip()
            )
            
            message_id = msg_obj.get("id", "UnknownID")
            from_id = None
            if "from_id" in msg_obj and msg_obj["from_id"]:
                from_id = msg_obj["from_id"].get("user_id", None)
                
            message_text = msg_obj.get("message", "")
            has_message = bool(message_text.strip())
            
            if has_message:
                message_text = html.escape(message_text)
            
            if has_quote and quote_text:
                quote_text_escaped = html.escape(quote_text)
                if has_message:
                    message_text = f"<pre>❝ {quote_text_escaped} ❞</pre>\n\n{message_text}"
                else:
                    message_text = f"<pre>❝ {quote_text_escaped} ❞</pre>"
                has_message = True

            final_caption = message_text
            
            # --- Upload Phase ---
            print(f"[Uploader] Processing message ID {message_id}...")
            
            did_send_media_msg = False
            
            if file_paths:
                # If there are multiple parts (due to splitting), send them sequentially.
                # Only the first part gets the big caption to avoid spam, others get a part indicator.
                for idx, file_path in enumerate(file_paths):
                    caption_to_use = final_caption if idx == 0 else f"Part {idx+1} for message {message_id}"
                    print(f"[Uploader] Sending media: {file_path}")
                    try:
                        input_file = await parallel_upload(upload_client, file_path)
                        await upload_client.send_file(
                            entity=dest_entity,
                            file=input_file,
                            caption=caption_to_use,
                            silent=True,
                            parse_mode="html"
                        )
                        did_send_media_msg = True
                        await asyncio.sleep(2) # Anti-flood
                    except Exception as e:
                        print(f"[Uploader Error] Failed sending {file_path}: {e}")
            
            if has_message and not did_send_media_msg:
                print(f"[Uploader] Forwarding pure text message ID {message_id}...")
                try:
                    await upload_client.send_message(
                        entity=dest_entity,
                        message=final_caption,
                        silent=True,
                        parse_mode="html"
                    )
                    await asyncio.sleep(2)
                except Exception as e:
                    print(f"[Uploader Error] Failed sending text for ID {message_id}: {e}")
                    
            
            # --- Resume Memorization ---
            # If everything succeeded, update the dump file to mark it as uploaded
            if msg_obj:
                msg_obj["is_uploaded"] = True
                updated_json_str = json.dumps(msg_obj, default=str)
                # It's safest to append the updated status to the end of the file. 
                # The script reading it will just use the latest entry for a given ID.
                with open(dump_file, "a", encoding="utf-8") as dump:
                    dump.write(updated_json_str + ",\n")
                    
        except Exception as e:
            print(f"[Uploader Error] Unhandled exception processing queue item: {e}")
            
        queue.task_done()


async def export_messages(
    download_client: TelegramClient,
    upload_client: TelegramClient,
    source_group_id: int,
    dest_group_id: int,
    mode: int,
    auto_resend: bool,
    use_stream: bool,
    min_id: int = 0,
    max_id: int = 0,
    min_size: int = 0,
    max_size: int = 0,
) -> None:
    """
    Exports messages and orchestrates the parallel backup and resend queues.
    """
    group: PeerChannel = await download_client.get_entity(PeerChannel(source_group_id))
    dump_file = os.path.join(output_folder, "dump_unified.json")
    
    # --- Parse Resume State ---
    processed_message_ids = set()
    if os.path.exists(dump_file):
        print("\n[Resume] Analyzing local dump file to find already processed messages...")
        with open(dump_file, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line: continue
                if line.endswith(","): line = line[:-1] # Remove trailing comma
                try:
                    obj = json.loads(line)
                    if obj.get("is_uploaded", False) or mode == 3:
                        # For text-only backups, mere presence in the dump means it's processed
                        processed_message_ids.add(obj.get("id"))
                except Exception:
                    # Ignore malformed JSON lines caused by abrupt script termination
                    pass
        print(f"[Resume] Found {len(processed_message_ids)} already processed/uploaded messages. These will be skipped.")
    
    file_mode: str = "a" if os.path.exists(dump_file) else "w"
    
    print("\n[Phase 1] Fetching all deleted events from Telegram...")
    all_events = []
    limit_per_request: int = 100
    
    try:
        current_max_id = max_id or 0
        while True:
            fetched_events = [
                event
                async for event in download_client.iter_admin_log(
                    group,
                    min_id=min_id or 0,
                    max_id=current_max_id,
                    limit=limit_per_request,
                    delete=True,
                )
            ]

            if not fetched_events:
                break
                
            valid_events = [e for e in fetched_events if e.deleted_message and e.old.id >= min_id]
            all_events.extend(valid_events)
            
            print(f"Fetched {len(valid_events)} events in this batch...")

            current_max_id = fetched_events[-1].id - 1
            if current_max_id < min_id:
                break
                
            await asyncio.sleep(0.5)
            
    except RPCError as e:
        print(f"RPCError during fetch: {e}")
        return

    print(f"\n[Phase 1 Complete] Found {len(all_events)} total deleted messages matching criteria.")
    if not all_events:
        return

    # Sort messages chronologically
    all_events.sort(key=lambda e: e.old.date)
    
    # Setup Asyncio Queue for Parallel Upload
    upload_queue = asyncio.Queue()
    
    # Start the consumer task if auto-resend is on
    uploader_task = None
    if auto_resend and dest_group_id and upload_client:
        print("[System] Starting background Uploader worker...")
        uploader_task = asyncio.create_task(uploader_worker(upload_client, upload_queue, dest_group_id))

    print("\n[Phase 2] Starting Parallel Downloader & Queueing...")
    
    with open(dump_file, file_mode, encoding="utf-8") as dump:
        c: int = 0
        m: int = 0
        
        for event in all_events:
            event_dict = event.old.to_dict()
            downloaded_files = []
            should_write = False
            
            # --- Resume Skip Logic ---
            if event.old.id in processed_message_ids:
                if auto_resend:
                    continue # Skip everything if it's already uploaded

            # --- Text/Media Filtering ---
            if mode == 1: # Export All (Text + Media)
                should_write = True
                if event.old.media:
                    m += 1
                    ext = utils.get_extension(event.old.media)

                    real_filename = f"{event.old.id}{ext}"
                    if hasattr(event.old.media, 'document') and hasattr(event.old.media.document, 'attributes'):
                        for attr in event.old.media.document.attributes:
                            if isinstance(attr, DocumentAttributeFilename):
                                real_filename = attr.file_name
                                break

                    file_path = os.path.join(output_folder, real_filename)

                    file_size = getattr(event.old.media.document, 'size', 0) if hasattr(event.old.media, 'document') and event.old.media.document else 0

                    # --- File Size Range Filter ---
                    if min_size > 0 and file_size < min_size:
                        print(f"[Filter] Skipping {real_filename} ({file_size / (1024*1024):.2f} MB) - below minimum size ({min_size / (1024*1024):.2f} MB)")
                        continue
                    if max_size > 0 and file_size > max_size:
                        print(f"[Filter] Skipping {real_filename} ({file_size / (1024*1024):.2f} MB) - exceeds maximum size ({max_size / (1024*1024):.2f} MB)")
                        continue

                    is_eligible_for_stream = use_stream and (file_size <= MAX_FILE_SIZE)
                    if use_stream and file_size > MAX_FILE_SIZE:
                        print(f"[Streamer] File {real_filename} is > 2GB ({(file_size/(1024**3)):.2f}GB). Falling back to disk storing and splitting.")

                    if auto_resend and dest_group_id and upload_client and is_eligible_for_stream:
                        print(f"[Streamer] Streaming media for ID {event.old.id} as {real_filename} directly to destination...")
                        dest_entity = await upload_client.get_entity(PeerChannel(dest_group_id))
                        # Build caption from original message so it's sent WITH the file
                        stream_caption = _build_caption(event_dict)
                        await stream_transfer(download_client, event.old, upload_client, dest_entity, caption=stream_caption)
                        downloaded_files = [f"streamed_part_{i}" for i in range(1, 10)] # Dummy list, not used for streaming
                    else:
                        already_downloaded = False
                        
                        # Smart Resume Check for Downloads
                        if os.path.exists(file_path) and os.path.getsize(file_path) > 0:
                             print(f"[Resume] File {real_filename} already exists locally. Skipping download.")
                             already_downloaded = True
                        elif os.path.exists(f"{file_path}.part1"):
                             print(f"[Resume] Split parts for {real_filename} already exist locally. Skipping download.")
                             already_downloaded = True
                             
                        if not already_downloaded:
                             print(f"[Downloader] Downloading media for ID {event.old.id} as {real_filename}...")
                             await parallel_download(download_client, event.old, file_path)
                             
                        # Always check if we need to split it or return its parts for the queue
                        # If already downloaded but not split yet (meaning script died purely mid-split), this safely handles it.
                        downloaded_files = await split_file_if_needed(file_path)
            
            elif mode == 2 and event.old.media:
                should_write = True
                m += 1
                ext = utils.get_extension(event.old.media)

                real_filename = f"{event.old.id}{ext}"
                if hasattr(event.old.media, 'document') and hasattr(event.old.media.document, 'attributes'):
                    for attr in event.old.media.document.attributes:
                        if isinstance(attr, DocumentAttributeFilename):
                            real_filename = attr.file_name
                            break

                file_path = os.path.join(output_folder, real_filename)

                file_size = getattr(event.old.media.document, 'size', 0) if hasattr(event.old.media, 'document') and event.old.media.document else 0

                # --- File Size Range Filter ---
                if min_size > 0 and file_size < min_size:
                    print(f"[Filter] Skipping {real_filename} ({file_size / (1024*1024):.2f} MB) - below minimum size ({min_size / (1024*1024):.2f} MB)")
                    continue
                if max_size > 0 and file_size > max_size:
                    print(f"[Filter] Skipping {real_filename} ({file_size / (1024*1024):.2f} MB) - exceeds maximum size ({max_size / (1024*1024):.2f} MB)")
                    continue

                is_eligible_for_stream = use_stream and (file_size <= MAX_FILE_SIZE)
                if use_stream and file_size > MAX_FILE_SIZE:
                    print(f"[Streamer] File {real_filename} is > 2GB ({(file_size/(1024**3)):.2f}GB). Falling back to disk storing and splitting.")

                if auto_resend and dest_group_id and upload_client and is_eligible_for_stream:
                    print(f"[Streamer] Streaming media for ID {event.old.id} as {real_filename} directly to destination...")
                    dest_entity = await upload_client.get_entity(PeerChannel(dest_group_id))
                    stream_caption = _build_caption(event_dict)
                    await stream_transfer(download_client, event.old, upload_client, dest_entity, caption=stream_caption)
                    downloaded_files = [f"streamed_part_{i}" for i in range(1, 10)] # Dummy list
                else:
                    already_downloaded = False
                    
                    # Smart Resume Check for Downloads
                    if os.path.exists(file_path) and os.path.getsize(file_path) > 0:
                         print(f"[Resume] File {real_filename} already exists locally. Skipping download.")
                         already_downloaded = True
                    elif os.path.exists(f"{file_path}.part1"):
                         print(f"[Resume] Split parts for {real_filename} already exist locally. Skipping download.")
                         already_downloaded = True
                         
                    if not already_downloaded:
                         print(f"[Downloader] Downloading media for ID {event.old.id} as {real_filename}...")
                         await parallel_download(download_client, event.old, file_path)
                         
                    downloaded_files = await split_file_if_needed(file_path)
            
            elif mode == 3 and not event.old.media:
                should_write = True
                c += 1
            
            if not should_write:
                continue
                
            # Inject saved target files into the JSON index memory so the resender can pinpoint them exactly
            event_dict['saved_files'] = [os.path.basename(f) for f in downloaded_files]
            
            # Dump to local unified file
            event_json_str = json.dumps(event_dict, default=str)
            dump.write(event_json_str + ",\n")

            # Produce to the queue for the uploader worker
            if auto_resend:
                if event.old.media and downloaded_files and downloaded_files[0].startswith("streamed_part_"):
                    # Media was already streamed directly WITH the caption attached,
                    # so nothing more to queue — skip duplicate text messages.
                    pass
                else:
                    # Queue with file paths for the uploader to handle
                    await upload_queue.put((event_json_str, downloaded_files))
                
        print("\n[Downloader] Finished downloading all targeted messages.")
        
    if auto_resend and uploader_task:
        print("[System] Waiting for background Uploader to finish uploading all files...")
        await upload_queue.put(None)  # Send shutdown signal
        await uploader_task  # Wait for uploader to terminate gracefully
        
    print("\n[Complete] All operations finished.")


async def main() -> None:
    """
    Main entry point.
    """
    parser = argparse.ArgumentParser(description="Unified Telegram Backup & Resend Sync")
    parser.add_argument("--mode", type=int, choices=[1, 2, 3], help="Export mode (1: All, 2: Media, 3: Text)")
    parser.add_argument("--min-id", type=int, default=0, help="Minimum message ID to export")
    parser.add_argument("--max-id", type=int, default=0, help="Maximum message ID to export")
    parser.add_argument("--min-size", type=str, default=None, help="Minimum file size (e.g., '100MB', '1GB'). Only for media exports.")
    parser.add_argument("--max-size", type=str, default=None, help="Maximum file size (e.g., '500MB', '2GB'). Only for media exports.")
    parser.add_argument("--auto-resend", action="store_true", help="Automatically resend downloaded messages to destination")
    parser.add_argument("--stream", action="store_true", help="Use direct memory streaming if auto-resending (no disk saves)")
    parser.add_argument("--disk", action="store_true", help="Use local disk for downloading before re-uploading")
    parser.add_argument("--auto-resume", action="store_true", help="Automatically set min-id based on previous saved dump logs")
    
    args = parser.parse_args()

    bot_token = os.getenv("BOT_TOKEN")
    use_bot_for_download = os.getenv("USE_BOT_FOR_DOWNLOAD", "false").lower() == "true"
    
    user_client = None
    bot_client = None
    
    if bot_token:
        bot_client = TelegramClient("tg_bot_session", api_id, api_hash)
        await bot_client.start(bot_token=bot_token)
        
        # Register the /status command listener
        @bot_client.on(events.NewMessage(pattern=r'^/status$'))
        async def status_handler(event):
            status_text = tracker.get_status_string()
            msg = await event.reply(status_text)
            
            # Start an auto-refresh loop in the background
            async def auto_refresh(target_msg):
                tracker_was_active = tracker.has_active_tasks()
                while tracker.has_active_tasks():
                    await asyncio.sleep(5)
                    try:
                        new_text = tracker.get_status_string()
                        if new_text != target_msg.text:
                            await target_msg.edit(new_text)
                            target_msg.text = new_text
                    except Exception as e:
                        if "Message is not modified" not in str(e):
                            print(f"[Status Refresh Error] {e}")
                            break

                # When loop finishes, do one final update to show empty state
                if tracker_was_active:
                    try:
                        await target_msg.edit("All transfers completed! ✅")
                    except:
                        pass

            asyncio.create_task(auto_refresh(msg))
            
    if not bot_token or not use_bot_for_download:
        user_client = TelegramClient("tg_session", api_id, api_hash)
        await user_client.start()
        
    download_client = bot_client if (bot_token and use_bot_for_download) else user_client
    upload_client = bot_client if bot_token else user_client
    
    print("\n" + "="*50)
    print("      🌊 TG MESSAGE LIFEGUARD - UNIFIED SYNC 🌊")
    print("="*50)
    
    src_id_input = source_group_env if source_group_env else input("\n📡 Enter SOURCE Group/Channel ID: ")
    src_id = int(src_id_input)
    
    # Mode
    if args.mode is not None:
        export_mode = args.mode
    else:
        print("\n📥 Export Modes:")
        print("   1 - Export All (Text + Media)")
        print("   2 - Export Media Only")
        print("   3 - Export Text Only")
        export_mode = int(input("👉 Select export mode (default 1): ") or 1)
    
    # Min/Max ID bounds
    min_id = args.min_id
    max_id = args.max_id
    
    # Auto-detect Resume capabilities from previous runs
    dump_file_path = os.path.join(output_folder, "dump_unified.json")
    max_saved_id = 0
    
    if os.path.exists(dump_file_path) and min_id == 0:
        try:
            with open(dump_file_path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line: continue
                    if line.endswith(","): line = line[:-1]
                    try:
                        obj = json.loads(line)
                        msg_id = obj.get("id")
                        if msg_id:
                            max_saved_id = max(max_saved_id, int(msg_id))
                    except Exception:
                        pass
        except Exception:
            pass
            
    if max_saved_id > 0 and min_id == 0:
        if args.auto_resume:
            min_id = max_saved_id + 1
            print(f"\n🔄 [Smart Resume] Auto-detected previous backup. Resuming seamlessly from Min ID: {min_id}")
        else:
            print(f"\n🔄 [Smart Resume] Detected previous backup up to Message ID {max_saved_id}")
            ans = input(f"   Do you want to automatically resume from here? (y/n, default y): ").strip().lower()
            if ans != 'n':
                min_id = max_saved_id + 1
                print(f"   -> Resuming gracefully from Min ID: {min_id}")
            
    if min_id == 0:
        min_id = int(input("\n🔢 Enter MIN message ID (0 for all): ") or 0)

    if max_id == 0:
        max_id = int(input("🔢 Enter MAX message ID (0 for all): ") or 0)

    # File Size Range Filter
    min_size_bytes = 0
    max_size_bytes = 0

    if args.min_size:
        try:
            min_size_bytes = _parse_size(args.min_size)
            print(f"\n📏 Minimum file size filter: {args.min_size} ({min_size_bytes / (1024*1024):.2f} MB)")
        except ValueError as e:
            print(f"\n⚠️ Warning: Invalid --min-size format: {e}")

    if args.max_size:
        try:
            max_size_bytes = _parse_size(args.max_size)
            print(f"📏 Maximum file size filter: {args.max_size} ({max_size_bytes / (1024*1024):.2f} MB)")
        except ValueError as e:
            print(f"\n⚠️ Warning: Invalid --max-size format: {e}")
    
    # Auto Resend 
    auto_resend = args.auto_resend
    if not auto_resend:
        # If not passed via CLI, optionally ask the user interactively
        auto_resend_ans = input("\n🚀 Do you want to automatically RESEND downloaded messages to a destination group? (y/n): ").strip().lower()
        auto_resend = auto_resend_ans == 'y'
        
    dest_id = None
    use_stream = False
    
    if auto_resend:
        dest_id_input = dest_group_env if dest_group_env else input("\n🎯 Enter DESTINATION Group/Channel ID: ")
        dest_id = int(dest_id_input)
        
        # Decide Streaming vs Disk Mode
        if args.stream:
            use_stream = True
        elif args.disk:
            use_stream = False
        else:
            print("\n⚡ Upload Engine Optimization ⚡")
            print("   S - Direct Memory Stream (Ultra-fast, skips saving to Local Hard Drive)")
            print("   D - Local Disk Backup (Downloads to hard drive first, then uploads)")
            choice = input("👉 Choose engine (S/D, default S): ").strip().lower()
            use_stream = (choice != 'd') # Defaults to streaming unless they explicitly say 'd'

    print("\n" + "="*50)
    print("🚀 INITIALIZING SYNC ENGINE...")
    print("="*50 + "\n")

    await export_messages(
        download_client=download_client,
        upload_client=upload_client,
        source_group_id=src_id,
        dest_group_id=dest_id,
        mode=export_mode,
        auto_resend=auto_resend,
        use_stream=use_stream,
        min_id=min_id,
        max_id=max_id,
        min_size=min_size_bytes,
        max_size=max_size_bytes,
    )


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n\n[System] Shutdown signal received. Exiting gracefully...")
