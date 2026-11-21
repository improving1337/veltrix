import asyncio
import io
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from PIL import Image
from telethon import functions, types
from telethon.errors import FloodWaitError, UsernameOccupiedError
from telethon.helpers import TotalList

import main
from profile_copy import DAY, DRAFT_TTL, ProfileCopies, available_username, copy_options, username_candidates


class Client:
    def __init__(self):
        self.members = [
            types.User(1, username='owner'),
            types.User(10, bot=True, username='helperbot'),
            types.User(20, username='source_one', first_name='Source', last_name='One'),
            types.User(30, username='source_two'),
        ]
        self.group = types.Chat(100, 'Group', types.ChatPhotoEmpty(), 4, None, 1)
        self.checked = []
        self.downloads = []
        self.photo_requests = []
        self.photo_ids = {}
        self.stills = {1: b'newest', 2: b'middle', 3: b'oldest'}
        self.variants = {}

    async def get_entity(self, key):
        return self.group

    async def __call__(self, request):
        if isinstance(request, functions.users.GetFullUserRequest):
            return SimpleNamespace(full_user=SimpleNamespace(about='Visible bio', birthday=types.Birthday(4 if getattr(request.id,'id',None)==20 else 5, 5)))
        if isinstance(request, functions.account.CheckUsernameRequest):
            self.checked.append(request.username)
            return True
        if isinstance(request, functions.messages.CheckChatInviteRequest):
            return types.ChatInviteAlready(self.group)
        raise AssertionError(type(request))

    async def iter_participants(self, entity, limit):
        for user in self.members:
            yield user

    async def get_profile_photos(self, user, limit):
        self.photo_requests.append((user.id, limit))
        ids = self.photo_ids.get(user.id, [1, 2, 3])
        result = TotalList(types.Photo(
            ident, 0, b'', None,
            [types.PhotoSize('m', 200, 200, 5), types.PhotoSize('x', 800, 800, 8)],
            1, video_sizes=[types.VideoSize('v', 1000, 1000, 20)]
        ) for ident in ids[:limit])
        result.total = len(ids)
        return result

    async def download_media(self, photo, file, *, thumb=None):
        self.downloads.append((photo.id, thumb))
        if thumb is None or thumb == 'v':
            return b'\x00\x00\x00\x14ftypmp42'
        return self.variants.get((photo.id, thumb), self.stills.get(photo.id))


class ProfileCopyTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.enterContext(patch('profile_copy.SEARCH_REQUEST_INTERVAL', 0))
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name) / 'profiles'
        self.client = Client()
        self.target = SimpleNamespace(me=types.User(1), client=self.client)
        self.second = SimpleNamespace(me=types.User(2), client=self.client)
        self.accounts = {'first': self.target, 'second': self.second}
        self.upload = AsyncMock()
        self.clear = AsyncMock()
        self.apply = AsyncMock(return_value=[])
        self.now = 1000
        self.service = self.make_service()
        self.data = {'group': 'https://t.me/group', 'fields': ['username', 'birthday', 'bio', 'avatars'],
                     'avatar_count': 3, 'delay_from_days': '1,5', 'delay_to_days': '1.5'}

    def make_service(self):
        return ProfileCopies(self.root, lambda: self.accounts, lambda image: image, self.upload, self.apply,
                             clock=lambda: self.now, clear_photos=self.clear)

    async def preview(self, target):
        with patch('profile_copy.random.shuffle'):
            return await self.service.preview(target, self.data)

    def test_readable_username_styles_are_valid_diverse_and_account_specific(self):
        for original in ('NorthWind77', 'silent_runner', 'a1', 'a' * 32):
            with self.subTest(original=original):
                candidates = username_candidates(original, '1')
                self.assertNotEqual(candidates[:5], username_candidates(original, '9')[:5])
                self.assertEqual(len(candidates), len(set(candidates)))
                self.assertNotIn(original.lower(), candidates)
                self.assertTrue(all(5 <= len(value) <= 32 and value[0].isalpha() and value[-1].isalpha()
                                    and not any(char.isdigit() for char in value) for value in candidates))
        self.assertEqual(username_candidates('123', '1'), [])

    async def test_username_check_skips_locally_reserved_and_retries_occupied_candidates(self):
        candidates = username_candidates('SilentRunner', '1')
        read = AsyncMock(side_effect=[UsernameOccupiedError(None), False, True])
        picked = await available_username(self.client, 'SilentRunner', '1', read, {candidates[0]})
        self.assertEqual(picked, candidates[3])
        self.assertEqual([call.args[1].username for call in read.await_args_list], candidates[1:4])

    async def test_preview_confirmation_spacing_persistence_and_source_uniqueness(self):
        draft = await self.preview(self.target)
        other_draft = await self.preview(self.second)
        self.assertEqual(draft['source_id'], '20')
        self.assertEqual(other_draft['source_id'], '30')
        self.assertEqual(draft['values'], {'first_name': 'Source', 'last_name': 'One', 'source_username': 'source_one',
                                          'username': self.client.checked[0], 'bio': 'Visible bio', 'birthday': '04.05'})
        self.assertNotEqual(draft['values']['username'], 'source_one')
        self.assertEqual(draft['delays'], [0, 1.5 * DAY, 1.5 * DAY])
        self.assertEqual(self.client.downloads[:3], [(3, 'x'), (2, 'x'), (1, 'x')])
        self.apply.assert_not_awaited()
        self.upload.assert_not_awaited()

        self.service.discard(self.second, other_draft['token'])
        self.assertFalse((self.service.photos / other_draft['token']).exists())
        job = await self.service.confirm(self.target, draft['token'])
        self.assertEqual(job['state'], 'waiting')
        self.assertEqual(job['due'], self.now + 1.5 * DAY)
        self.apply.assert_awaited_once_with(self.target, draft['values'])
        self.clear.assert_awaited_once_with(self.target)
        self.upload.assert_awaited_once_with(self.target, b'oldest')
        with self.assertRaises(ValueError):
            await self.service.confirm(self.target, draft['token'])

        self.service = self.make_service()
        newer = await self.preview(self.second)
        self.assertEqual(newer['source_id'], '30')
        await self.service.advance('1', self.target)
        self.upload.assert_awaited_once()
        self.now = job['due'] + 300
        await self.service.advance('1', self.target)
        self.upload.assert_awaited_with(self.target, b'middle')
        self.now += 1.5 * DAY
        await self.service.advance('1', self.target)
        self.upload.assert_awaited_with(self.target, b'newest')
        self.assertEqual(self.service.status(self.target)['state'], 'done')
        self.assertEqual(self.service.status(self.target)['uploaded'], 3)
        self.assertFalse((self.service.photos / draft['token']).exists())
        self.assertEqual(self.make_service().used_sources, {'20'})

    async def test_expired_draft_and_interrupted_upload(self):
        draft = await self.preview(self.target)
        self.now += DRAFT_TTL
        with self.assertRaisesRegex(ValueError, 'истёк'):
            await self.service.confirm(self.target, draft['token'])
        self.assertFalse((self.service.photos / draft['token']).exists())
        draft = await self.preview(self.target)
        self.upload.side_effect = asyncio.CancelledError()
        with self.assertRaises(asyncio.CancelledError):
            await self.service.confirm(self.target, draft['token'])
        restarted = self.make_service()
        self.assertEqual(restarted.status(self.target)['state'], 'interrupted')
        await restarted.advance('1', self.target)
        self.upload.assert_awaited_once()
        self.assertFalse((restarted.photos / draft['token']).exists())

    async def test_http_decline_and_target_bound_photo_access(self):
        self.data['group'] = 'https://t.me/+PrivateLink'
        app = web.Application(middlewares=[main.error_mw])
        self.service.register(app, lambda request: self.accounts[request.match_info['acc']])
        async with TestClient(TestServer(app)) as http:
            base = '/api/accounts/first/profile-copy'
            with patch('profile_copy.random.shuffle'):
                response = await http.post(base, json=self.data)
            self.assertEqual(response.status, 200)
            draft = await response.json()
            route = '/' + draft['token']
            path = route + '/photos/0'
            response = await http.get(base + path)
            self.assertEqual(await response.read(), b'oldest')
            response = await http.get('/api/accounts/second/profile-copy' + path)
            self.assertEqual(response.status, 404)
            response = await http.delete(base + route)
            self.assertEqual(response.status, 200)
            self.apply.assert_not_awaited()
            self.upload.assert_not_awaited()
            self.assertEqual(self.service.used_sources, set())

    def test_options_validate_avatar_delays_only_when_selected(self):
        for minimum, maximum in [(0, 2), (3, 2), ('NaN', 2), ('invalid', 3)]:
            with self.assertRaises(ValueError):
                copy_options({**self.data, 'delay_from_days': minimum, 'delay_to_days': maximum})
        self.assertEqual(copy_options({'group':'', 'fields':[]})[1], 'minecraft_beseda764')
        self.assertEqual(copy_options({**self.data, 'avatar_count': 1, 'delay_from_days': ''})[-1], None)
        for count in ('', 0, -1, 2.5, True):
            with self.subTest(count=count), self.assertRaises(ValueError):
                copy_options({**self.data, 'avatar_count': count})

    async def test_pick_profile_with_exact_number_of_photos(self):
        self.client.members.append(types.User(40, username='source_three'))
        self.client.photo_ids = {20: [1, 2], 30: [1, 2, 3, 4], 40: [1, 2, 3]}
        draft = await self.preview(self.target)
        self.assertEqual(draft['source_id'], '40')
        self.assertEqual(len(draft['delays']), 3)
        self.assertEqual(self.client.photo_requests, [(20, 4), (30, 4), (40, 4)])
        self.assertEqual(self.client.downloads, [(3, 'x'), (2, 'x'), (1, 'x')])

    async def test_search_continues_after_first_sixty_candidates(self):
        self.client.members = [types.User(ident) for ident in range(100, 164)]
        self.client.photo_ids = {ident: [1] for ident in range(100, 163)}
        self.data['fields'] = ['avatars']
        draft = await self.preview(self.target)
        self.assertEqual(draft['source_id'], '163')
        self.assertEqual(len(self.client.photo_requests), 64)

    async def test_avatar_range_and_unchecked_optional_profile_fields(self):
        self.client.photo_ids[20] = [1, 2]
        self.data = {'group': '', 'fields': ['avatars'], 'avatar_min': 3, 'avatar_max': 5}
        draft = await self.preview(self.target)
        self.assertEqual(draft['source_id'], '30')
        self.assertEqual(len(draft['delays']), 3)
        self.assertIn('bio', draft['values'])
        self.assertIn('birthday', draft['values'])
        self.assertEqual(self.client.downloads, [(3, 'x'), (2, 'x'), (1, 'x')])

    async def test_similar_profile_is_skipped(self):
        self.second.me.username = 'source_one_12345'
        draft = await self.preview(self.target)
        self.assertEqual(draft['source_id'], '30')

    async def test_direct_copy_with_music_and_all_avatars(self):
        document = types.Document(12, 34, b'reference', None, 'audio/mpeg', 1, 1, [])
        original = self.client.__class__.__call__

        async def request(client, req):
            if isinstance(req, functions.users.GetSavedMusicRequest):
                return types.users.SavedMusic(1, [document])
            return await original(client, req)

        self.client.get_entity = AsyncMock(return_value=self.client.members[2])
        self.data.update(mode='profile', group='https://t.me/source_one', fields=[], avatar_count=1)
        with patch.object(Client, '__call__', request):
            draft = await self.preview(self.target)
        self.assertEqual(len(draft['delays']), 3)
        self.assertEqual(self.service.public_draft(draft)['values']['music'], 1)
        self.assertEqual(draft['values']['music'][0]['id'], 12)

    async def test_search_waits_for_telegram_then_resumes_same_read(self):
        self.client.photo_ids[20] = [1, 2]
        get_photos = self.client.get_profile_photos
        attempted = []

        async def limited_photos(user, limit):
            attempted.append(user.id)
            if attempted == [20, 30]:
                raise FloodWaitError(None, capture=1)
            return await get_photos(user, limit)

        with patch.object(self.client, 'get_profile_photos', limited_photos), patch('profile_copy.random.shuffle'):
            app = web.Application(middlewares=[main.error_mw])
            self.service.register(app, lambda request: self.accounts[request.match_info['acc']])
            async with TestClient(TestServer(app)) as http:
                base = '/api/accounts/first/profile-copy/search'
                reply = await http.post(base, json=self.data)
                started = await reply.json()
                route = base + '/' + started['token']
                async with asyncio.timeout(6):
                    while True:
                        status = await (await http.get(route)).json()
                        if status['state'] == 'waiting':
                            break
                        await asyncio.sleep(.02)
                    self.assertEqual(status['checked'], 2)
                    self.assertGreaterEqual(status['retry_in'], 1)
                    self.assertEqual(attempted, [20, 30])
                    duplicate = await (await http.post(base, json=self.data)).json()
                    self.assertEqual(duplicate['token'], started['token'])
                    other = '/api/accounts/second/profile-copy/search/' + started['token']
                    self.assertIsNone(await (await http.get(other)).json())
                    while True:
                        status = await (await http.get(route)).json()
                        if status['state'] == 'ready':
                            break
                        self.assertIn(status['state'], ('waiting', 'searching'))
                        await asyncio.sleep(.25)
                self.assertEqual(status['draft']['source_id'], '30')
                self.assertEqual(attempted, [20, 30, 30])
                self.upload.assert_not_awaited()
                self.apply.assert_not_awaited()

    async def test_video_avatar_download_uses_still_sizes_and_retries_bad_image(self):
        self.service.prepare_avatar = main.prepare_avatar
        self.data.update(avatar_count=1, fields=['avatars'])
        self.client.photo_ids[20] = [1]
        image = io.BytesIO()
        Image.new('RGB', (40, 40), 'blue').save(image, format='JPEG')
        self.client.variants = {(1, 'x'): b'damaged download', (1, 'm'): image.getvalue()}
        draft = await self.preview(self.target)
        self.assertEqual(draft['delays'], [0])
        self.assertEqual(self.client.downloads, [(1, 'x'), (1, 'm')])
        with Image.open(self.service.photos / draft['token'] / '0.jpg') as prepared:
            prepared.verify()

    async def test_unreadable_photos_skip_candidate_and_report_cleanly_if_exhausted(self):
        self.service.prepare_avatar = main.prepare_avatar
        self.data.update(avatar_count=1, fields=['avatars'])
        self.client.photo_ids.update({20: [1], 30: [2]})
        with self.assertRaisesRegex(ValueError, 'скачать все их аватарки не удалось'):
            await self.preview(self.target)
        self.assertEqual(self.client.downloads, [(1, 'x'), (1, 'm'), (2, 'x'), (2, 'm')])
        self.assertEqual(list(self.service.photos.iterdir()), [])

    async def test_apply_fields_changes_only_selected_fields(self):
        client = AsyncMock(return_value=True)
        client.get_me = AsyncMock(return_value=self.target.me)
        acc = SimpleNamespace(me=self.target.me, client=client)
        with (patch('main.invalidate_account_profile'), patch('main.schedule_account_profile_refresh'),
              patch('main.set_public', AsyncMock(return_value=[])) as public,
              patch('main.set_last_seen_hidden', AsyncMock(return_value=[])) as hidden):
            errors = await main.copy_profile_fields(acc, {'birthday': '04.05', 'bio': 'Visible bio'})
        self.assertEqual(errors, [])
        public.assert_awaited_once_with(acc, ['bio', 'birthday', 'photo'])
        hidden.assert_awaited_once_with(acc, True)
        requests = [call.args[0] for call in client.await_args_list]
        self.assertEqual([type(request) for request in requests],
                         [functions.account.UpdateProfileRequest, functions.account.UpdateBirthdayRequest])
        self.assertIsNone(requests[0].first_name)
        self.assertEqual(requests[0].about, 'Visible bio')
        self.assertEqual(requests[1].birthday.month, 5)


if __name__ == '__main__':
    unittest.main()
