import base64
import enum
import json
from typing import Callable, Optional, Union

from tidal_async.utils import snake_to_camel, parse_title, id_from_url
from tidal_async.exceptions import InvalidURL

try:
    from httpseekablefile import AsyncSeekableHTTPFile
except ImportError:
    pass


# TODO [#1]: Artist object


class AudioQuality(enum.Enum):
    Normal = "LOW"
    High = "HIGH"
    HiFi = "LOSSLESS"
    Master = "HI_RES"


class AudioMode(enum.Enum):
    # TODO [#2]: Find more audio modes
    Stereo = "STEREO"


class Cover(object):
    def __init__(self, tidal_session, id_):
        self.sess = tidal_session
        self.id = id_

    def url(self, size=(640, 640)):
        # Valid resolutions: 80x80, 160x160, 320x320, 640x640, 1280x1280
        return f"https://resources.tidal.com/images/{self.id.replace('-', '/')}/{size[0]}x{size[1]}.jpg"

    if 'AsyncSeekableHTTPFile' in globals():
        async def get_async_file(self, filename: Optional[str] = None, size=(640, 640)):
            return await AsyncSeekableHTTPFile.create(self.url(size), filename, self.sess.sess)


class TidalObject(object):
    def __init__(self, tidal_session, dict_):
        self.sess = tidal_session
        self.dict = dict_

    async def reload_info(self):
        raise NotImplemented

    @classmethod
    async def from_id(cls, tidal_session, id_):
        obj = cls(tidal_session, {'id': id_})
        await obj.reload_info()
        return obj

    @classmethod
    async def _from_url(cls, tidal_session, url):
        for child_cls in cls.__subclasses__():
            try:
                if hasattr(child_cls, 'urlname'):
                    print(child_cls.urlname, id_from_url(url, child_cls.urlname))
                    return await child_cls.from_id(tidal_session, id_from_url(url, child_cls.urlname))
            except InvalidURL:
                pass

        # If none objects match url, then the url must be invalid
        raise InvalidURL

    @classmethod
    async def from_url(cls, tidal_session, url):
        if cls is TidalObject:
            return await cls._from_url(tidal_session, url)

        if hasattr(cls, 'urlname'):
            return await cls.from_id(tidal_session, id_from_url(url, cls.urlname))

        # Called class has no field urlname so from_url is not implemented
        raise NotImplemented

    def __getattr__(self, attr):
        return self.dict.get(snake_to_camel(attr))

    def __contains__(self, item):
        return snake_to_camel(item) in self.dict


# TODO [#3]: Downloading lyrics
class Track(TidalObject):
    urlname = 'track'

    async def reload_info(self):
        resp = await self.sess.get(f"/v1/tracks/{self.id}", params={
            "countryCode": self.sess.country_code
        })
        self.dict = await resp.json()

    @property
    def album(self):
        return Album(self.sess, self.dict['album'])

    @property
    def cover(self):
        return self.album.cover

    @property
    def audio_quality(self):
        return AudioQuality(self.dict['audioQuality'])

    async def _playbackinfopostpaywall(self, audio_quality=AudioQuality.Master):
        resp = await self.sess.get(f"/v1/tracks/{self.id}/playbackinfopostpaywall", params={
            "playbackmode": "STREAM", "assetpresentation": "FULL",
            "audioquality": audio_quality.value
        })

        return await resp.json()

    async def _stream_manifest(self, audio_quality=AudioQuality.Master):
        data = await self._playbackinfopostpaywall(audio_quality)
        return json.loads(base64.b64decode(data['manifest']))
    
    async def get_stream_url(self, audio_quality=AudioQuality.Master):
        # TODO [$5f4d572852d59c0007de0010]: [Track.get_stream_url] Raise exception when audio quality worse than min_audio_quality
        #   eg. InsufficientAudioQuality
        # TODO [$5f4d572852d59c0007de0011]: [Track.get_stream_url] Allow to specify min_audio_quality in per-session basics
        return (await self._stream_manifest(audio_quality))['urls'][0]

    async def get_metadata_tags(self):
        album = self.album
        await album.reload_info()

        tags = {
            # general metatags
            'artist': self.artist['name'],
            'title': parse_title(self, self.artists),

            # album related metatags
            'albumartist': album.artist['name'],
            'album': parse_title(album),
            'date': str(album.year),

            # track/disc position metatags
            'discnumber': str(self.volumeNumber),
            'disctotal': str(album.numberOfVolumes),
            'tracknumber': str(self.trackNumber),
            'tracktotal': str(album.numberOfTracks)
        }

        # Tidal sometimes returns null for track copyright
        if 'copyright' in self and self.copyright:
            tags['copyright'] = self.copyright
        elif 'copyright' in album and album.copyright:
            tags['copyright'] = album.copyright

        # identifiers for later use in own music libraries
        if 'isrc' in self and self.isrc:
            tags['isrc'] = self.isrc
        if 'upc' in album and album.upc:
            tags['upc'] = album.upc

        return tags

    if 'AsyncSeekableHTTPFile' in globals():
        async def get_async_file(self, filename: Optional[Union[Callable[['Track'], str], str]] = None,
                                 audio_quality=AudioQuality.Master):
            if callable(filename):
                filename = filename(self)
            elif filename is None:
                filename = self.title
            return await AsyncSeekableHTTPFile.create(await self.get_stream_url(audio_quality), filename, self.sess.sess)


class Playlist(TidalObject):
    urlname = 'playlist'

    async def reload_info(self):
        resp = await self.sess.get(f"/v1/playlists/{self.id}", params={
            "countryCode": self.sess.country_code
        })

        # NOTE: I'm updating self.dict and not reassingning it as the return ftom the api does not contain the `id` key
        self.dict.update(await resp.json())

    @property
    def cover(self):
        # NOTE: It may be also self.dict['squareImage'], needs testing
        return Cover(self.sess, self.dict['image'])

    async def _fetch_items(self, items=None, offset=0):
        # TODO [#11]: Make Playlist._fetch_items call just one request
        #   Playlist.tracks should call _fetch_items multiple times in LOOP (don't do recursion like that PLZ @wvffle)
        limit = 50  # Limit taken from the request done by tidal website

        resp = await self.sess.get(f"/v1/playlists/{self.id}/items", params={
            "countryCode": self.sess.country_code,
            "offset": offset,
            "limit": limit,
        })

        json = await resp.json()

        if offset + len(json['items']) >= json['totalNumberOfItems']:
            return items + json['items']

        # @wvffle, get this recursion out of here plz
        return await self._fetch_items(items + json['items'], offset + limit)

    async def tracks(self):
        # TODO [#12]: Convert Playlist.tracks to generator and don't load all tracks on the time
        if 'items' not in self.dict:
            self.dict['items'] = await self._fetch_items()

        tracks = []
        for item in self.dict['items']:
            if item['type'] == 'track':
                tracks.append(item['item'])

        return [Track(self.sess, track) for track in tracks]


class Album(TidalObject):
    urlname = 'album'

    async def reload_info(self):
        resp = await self.sess.get(f"/v1/albums/{self.id}", params={
            "countryCode": self.sess.country_code
        })

        self.dict = await resp.json()

    @property
    def cover(self):
        return Cover(self.sess, self.dict['cover'])

    async def tracks(self):
        # TODO [#13]: Convert Album.tracks to generator
        if 'items' not in self.dict:
            resp = await self.sess.get(f"/v1/albums/{self.id}/tracks", params={
                "countryCode": self.sess.country_code
            })

            self.dict.update(await resp.json())

        return [Track(self.sess, track) for track in self.dict['items']]
