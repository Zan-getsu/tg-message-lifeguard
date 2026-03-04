import json
import time
import glob
from telethon import TelegramClient
from telethon.tl.types import PeerChannel
from datetime import datetime
import asyncio
import html
import os
from dotenv import load_dotenv

from src.utils.fast_telethon import parallel_upload

load_dotenv()

input_folder: str = "backup_will_be_inside_me"


async def main():
    with open(os.path.join(input_folder, "dump.json"), "r") as file:
        raw = file.read()
        if raw.endswith(","):
            raw = raw[:-1]
        fixed = "[" + raw + "]"
        content = json.loads(fixed)
        content = sorted(content, key=lambda x: x["date"])

        api_id_env = os.getenv("API_ID")
        api_hash_env = os.getenv("API_HASH")
        chat_id_env = os.getenv("DESTINATION_GROUP_ID")
        bot_token_env = os.getenv("BOT_TOKEN")
        
        api_id = int(api_id_env) if api_id_env else int(input("Enter your api_id: "))
        api_hash = api_hash_env if api_hash_env else input("Enter your api_hash: ")
        chat_id = int(chat_id_env) if chat_id_env else int(input("Enter your DESTINATION_GROUP_ID: "))

        if bot_token_env:
            client = TelegramClient("tg_bot_session", api_id, api_hash)
            await client.start(bot_token=bot_token_env)
        else:
            client = TelegramClient("tg_session", api_id, api_hash)
            await client.start()

        group = await client.get_entity(PeerChannel(int(chat_id)))

        for msg in content:
            if msg["_"] != "Message":
                continue
            peer_id = msg["peer_id"].get("channel_id", None)
            reply_to_msg_id = msg["reply_to"].get("reply_to_msg_id", None)
            reply_to_top_id = msg["reply_to"].get("reply_to_top_id", None)
            reply_to = "/".join(
                str(s).strip()
                for s in [peer_id, reply_to_top_id, reply_to_msg_id]
                if s and str(s).strip())
            message_id = msg["id"]
            from_id = msg["from_id"].get("user_id", None)
            message = msg.get("message", "")
            has_media = msg.get("media", None) is not None
            has_message = message != ""
            date = datetime.fromisoformat(msg["date"]).strftime("%Y %b %d, %H:%M")

            print(
                f"{message_id} {message}, {date}, has_media: {has_media}, from_id: {from_id}, reply_to: {reply_to}"
            )

            if msg["reply_to"]["quote"]:
                quote_text = msg["reply_to"].get("quote_text", None)
                if quote_text is not None:
                    quote_text = html.escape(quote_text)
                    if has_message:
                        message = f"<pre>❝ {quote_text} ❞</pre>\n\n{message}"
                    else:
                        message = f"<pre>❝ {quote_text} ❞</pre>"
                    has_message = True

            # The original message variable already contains the quote and message text
            # We will use it directly without appending the date, user, and reply link.

            did_send_media_msg = False

            if has_media:
                file_names = glob.glob(f"{input_folder}/{message_id}.*")
                for file_name in file_names:
                    print(f"Sending Media: {file_name}")
                    try:
                        input_file = await parallel_upload(client, file_name)
                        await client.send_file(entity=group,
                                               file=input_file,
                                               caption=message,
                                               silent=True)
                        did_send_media_msg = True
                    except Exception as e:
                        print(f"Error sending media {file_name}: {str(e)}")

            if has_message and not did_send_media_msg:
                print(f"Sending Message: {message}")
                try:
                    await client.send_message(entity=group,
                                              message=message,
                                              silent=True,
                                              parse_mode="html")
                except Exception as e:
                    print(f"Error sending message: {str(e)}")

            time.sleep(2)


if __name__ == "__main__":
    asyncio.run(main())
