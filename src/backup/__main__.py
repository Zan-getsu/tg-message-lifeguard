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

from src.utils.fast_telethon import parallel_download, parallel_upload

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


def split_file_if_needed(filepath: str) -> list[str]:
    """
    Checks if a file exceeds the 1.95GB limit and splits it into byte chunks if needed.
    Returns a list of file paths (either the original file, or the newly split parts).
    """
    if not os.path.exists(filepath):
        return []
    
    file_size = os.path.getsize(filepath)
    if file_size <= MAX_FILE_SIZE:
        return [filepath]
    
    print(f"\n[Splitter] File {filepath} is {(file_size / (1024**3)):.2f}GB. Splitting into {MAX_FILE_SIZE / (1024**3):.2f}GB parts...")
    
    part_files = []
    part_num = 1
    
    with open(filepath, 'rb') as f_in:
        while True:
            chunk = f_in.read(MAX_FILE_SIZE)
            if not chunk:
                break
            
            part_filename = f"{filepath}.part{part_num}"
            with open(part_filename, 'wb') as f_out:
                f_out.write(chunk)
                
            part_files.append(part_filename)
            print(f"[Splitter] Created chunk: {part_filename}")
            part_num += 1
            
    # We can either delete the original huge file to save space, or keep it.
    # To save space for the user, we will remove the huge original file since it's now split.
    os.remove(filepath)
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
                try:
                    await upload_client.send_message(
                        entity=dest_entity,
                        message=final_caption,
                        silent=True,
                        parse_mode="html"
                    )
                    await asyncio.sleep(2)
                except Exception as e:
                    print(f"[Uploader Error] Failed sending text: {e}")
                    
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
    min_id: int = 0,
    max_id: int = 0,
) -> None:
    """
    Exports messages and orchestrates the parallel backup and resend queues.
    """
    group: PeerChannel = await download_client.get_entity(PeerChannel(source_group_id))
    dump_file = os.path.join(output_folder, "dump_unified.json")
    
    file_mode: str = "a" if os.path.exists(dump_file) else "w"
    
    print("\n[Phase 1] Fetching all deleted events from Telegram...")
    all_events = []
    limit_per_request: int = 100
    
    try:
        current_max_id = max_id or 0
        while True:
            events = [
                event
                async for event in download_client.iter_admin_log(
                    group,
                    min_id=min_id or 0,
                    max_id=current_max_id,
                    limit=limit_per_request,
                    delete=True,
                )
            ]

            if not events:
                break
                
            valid_events = [e for e in events if e.deleted_message and e.old.id >= min_id]
            all_events.extend(valid_events)
            
            print(f"Fetched {len(valid_events)} events in this batch...")

            current_max_id = events[-1].id - 1
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
                    print(f"[Downloader] Downloading media for ID {event.old.id} as {real_filename}...")
                    await parallel_download(download_client, event.old.media, file_path)
                    downloaded_files = split_file_if_needed(file_path)
            
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
                print(f"[Downloader] Downloading media for ID {event.old.id} as {real_filename}...")
                await parallel_download(download_client, event.old.media, file_path)
                downloaded_files = split_file_if_needed(file_path)
            
            elif mode == 3 and not event.old.media:
                should_write = True
                c += 1
            
            if not should_write:
                continue
                
            # Inject saved target files into the JSON index memory so the resender can pinpoint them exactly
            event_dict['saved_files'] = [os.path.basename(f) for f in downloaded_files]
            
            # Dump to local unified file
            import json
            event_json_str = json.dumps(event_dict, default=str)
            dump.write(event_json_str + ",\n")

            # Produce to the queue
            if auto_resend:
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
    parser.add_argument("--auto-resend", action="store_true", help="Automatically resend downloaded messages to destination")
    
    args = parser.parse_args()

    bot_token = os.getenv("BOT_TOKEN")
    use_bot_for_download = os.getenv("USE_BOT_FOR_DOWNLOAD", "false").lower() == "true"
    
    user_client = None
    bot_client = None
    
    if bot_token:
        bot_client = TelegramClient("tg_bot_session", api_id, api_hash)
        await bot_client.start(bot_token=bot_token)
    
    if not bot_token or not use_bot_for_download:
        user_client = TelegramClient("tg_session", api_id, api_hash)
        await user_client.start()
        
    download_client = bot_client if (bot_token and use_bot_for_download) else user_client
    upload_client = bot_client if bot_token else user_client
    
    print("\n--- Unified Telegram Sync ---")
    
    src_id_input = source_group_env if source_group_env else input("Enter SOURCE Group/Channel ID: ")
    src_id = int(src_id_input)
    
    # Mode
    if args.mode is not None:
        export_mode = args.mode
    else:
        print("\n1 - Export All (Text + Media)")
        print("2 - Export Media Only")
        print("3 - Export Text Only")
        export_mode = int(input("Enter export mode: ") or 1)
    
    # Min/Max ID bounds
    min_id = args.min_id if args.min_id != 0 else int(input("\nEnter min message ID (0 for all): ") or 0)
    max_id = args.max_id if args.max_id != 0 else int(input("Enter max message ID (0 for all): ") or 0)
    
    # Auto Resend 
    auto_resend = args.auto_resend
    if not auto_resend:
        # If not passed via CLI, optionally ask the user interactively hook
        auto_resend_ans = input("\nDo you want to automatically resend downloaded messages to a destination group? (y/n): ").strip().lower()
        auto_resend = auto_resend_ans == 'y'
        
    dest_id = None
    if auto_resend:
        dest_id_input = dest_group_env if dest_group_env else input("Enter DESTINATION Group/Channel ID: ")
        dest_id = int(dest_id_input)
        
    await export_messages(
        download_client=download_client,
        upload_client=upload_client,
        source_group_id=src_id,
        dest_group_id=dest_id,
        mode=export_mode,
        auto_resend=auto_resend,
        min_id=min_id,
        max_id=max_id
    )


if __name__ == "__main__":
    loop = asyncio.get_event_loop()
    loop.run_until_complete(main())
