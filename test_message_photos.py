import io
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from aiohttp import FormData, web
from aiohttp.test_utils import TestClient, TestServer
from PIL import Image
from telethon import types

import main
import scenarios


def image_data():
    output = io.BytesIO()
    Image.new("RGB", (32, 24), "blue").save(output, "PNG")
    return output.getvalue()


def upload(data, caption=None):
    form = FormData()
    form.add_field("photo", data, filename="clipboard.png", content_type="image/png")
    if caption is not None:
        form.add_field("text", caption)
    return form


class PhotoTests(unittest.IsolatedAsyncioTestCase):
    async def test_chat_sends_photo_with_caption_and_preserves_text_route(self):
        sent = SimpleNamespace(id=10)
        client = AsyncMock()
        client.send_file.return_value = sent
        client.send_message.return_value = sent
        acc = SimpleNamespace(name="account", client=client)
        app = web.Application()
        app.router.add_post("/api/accounts/{acc}/chats/{chat_id}/send", main.api_send)
        with (patch.object(main, "get_acc", return_value=acc),
              patch.object(main, "resolve", AsyncMock(return_value="peer")),
              patch.object(main, "ser_message", return_value={"id": 10})):
            async with TestClient(TestServer(app)) as http:
                url = "/api/accounts/account/chats/1/send"
                response = await http.post(url, data=upload(b"invalid image"))
                self.assertEqual(response.status, 400)
                client.send_file.assert_not_awaited()
                response = await http.post(url, data=upload(image_data(), "Подпись"))
                self.assertEqual(await response.json(), {"message": {"id": 10}})
                response = await http.post(url, json={"text": "Привет"})
                self.assertEqual(response.status, 200)
        args = client.send_file.await_args
        self.assertEqual(args.args[1].getvalue()[:2], b"\xff\xd8")
        self.assertEqual(args.kwargs, {"caption": "Подпись", "force_document": False})
        client.send_message.assert_awaited_once_with("peer", "Привет")

    async def test_scenario_keeps_random_selection_and_photo_after_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            pool = {str(i): SimpleNamespace(me=types.User(i), client=SimpleNamespace(is_connected=lambda: True))
                    for i in range(1, 14)}
            service = scenarios.Scenarios(directory, lambda: pool)
            service.launch_worker = Mock()
            app = web.Application()
            service.register(app)
            async with TestClient(TestServer(app)) as http:
                response = await http.post("/api/scenarios/photo", data=upload(image_data()))
                image = (await response.json())["image"]
                payload = {"image": image, "code": '''
for acc in random_accounts(all, 10):
    start(acc, "https://t.me/samplebot?start=abc")
    send_photo(acc, "@samplebot", photo())
    wait(1)
    bot_button(acc, "@samplebot", 1, text="❤️")
'''}
                preview = await (await http.post("/api/scenarios/preview", json=payload)).json()
                self.assertEqual(preview["steps"][1]["args"]["image"], image)
                self.assertEqual(len({step["account"] for step in preview["steps"] if step["action"] == "start"}), 10)
                response = await http.post("/api/scenarios/start", json=payload | {
                    "seed": preview["seed"], "token": preview["token"]
                })
                self.assertEqual(response.status, 200)
            resumed = scenarios.Scenarios(directory, lambda: pool)
            self.assertEqual(resumed.state["run"]["steps"], preview["steps"])
            self.assertTrue(resumed.image_path(image).is_file())

    async def test_bot_click_matches_newest_incoming_reply_after_photo(self):
        with tempfile.TemporaryDirectory() as directory:
            service = scenarios.Scenarios(directory, lambda: {})
            service.state["run"] = {}
            asset = "a" * 64
            service.images.mkdir(parents=True)
            (service.images / (asset + ".jpg")).write_bytes(b"photo")
            step = {"account": 1, "target": "@samplebot", "action": "send_photo",
                    "args": {"peer": "@samplebot", "image": asset, "caption": ""}}
            client = AsyncMock()
            client.send_file.return_value = SimpleNamespace(id=20)
            client.get_entity.return_value = SimpleNamespace(bot=True)
            old = SimpleNamespace(click=AsyncMock(), text="❤️")
            heart = SimpleNamespace(click=AsyncMock(), text="❤",
                                    button=SimpleNamespace(type=types.InlineButtonTypeCallback(b"click")))
            client.get_messages.return_value = [
                SimpleNamespace(id=19, out=False, buttons=[[old]]),
                SimpleNamespace(id=20, out=True),
                SimpleNamespace(id=21, out=False, buttons=[[heart]]),
            ]
            extra = {"wait_before_action": AsyncMock(return_value=False)}
            self.assertEqual((await service.execute(step, "1", client, extra))[0], "ok")
            click = step | {"action": "bot_button", "args": {"peer": "@samplebot", "position": 1, "text": "❤️"}}
            self.assertEqual((await service.execute(click, "1", client, extra))[0], "ok")
            heart.click.assert_awaited_once()
            old.click.assert_not_awaited()
