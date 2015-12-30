# -*- coding: utf-8 -*-
# This file is part of beets.
# Copyright 2016, Adrian Sampson.
#
# Permission is hereby granted, free of charge, to any person obtaining
# a copy of this software and associated documentation files (the
# "Software"), to deal in the Software without restriction, including
# without limitation the rights to use, copy, modify, merge, publish,
# distribute, sublicense, and/or sell copies of the Software, and to
# permit persons to whom the Software is furnished to do so, subject to
# the following conditions:
#
# The above copyright notice and this permission notice shall be
# included in all copies or substantial portions of the Software.

"""Adds Discogs album search support to the autotagger. Requires the
discogs-client library.
"""
from __future__ import (division, absolute_import, print_function,
                        unicode_literals)

import beets.ui
from beets import logging
from beets import config
from beets.autotag.hooks import AlbumInfo, TrackInfo, Distance
from beets.plugins import BeetsPlugin
from beets.util import confit
from discogs_client import Release, Client
from discogs_client.exceptions import DiscogsAPIError
from requests.exceptions import ConnectionError
import beets
import re
import time
import json
import socket
import httplib
import os


# Silence spurious INFO log lines generated by urllib3.
urllib3_logger = logging.getLogger('requests.packages.urllib3')
urllib3_logger.setLevel(logging.CRITICAL)

USER_AGENT = u'beets/{0} +http://beets.radbox.org/'.format(beets.__version__)

# Exceptions that discogs_client should really handle but does not.
CONNECTION_ERRORS = (ConnectionError, socket.error, httplib.HTTPException,
                     ValueError,  # JSON decoding raises a ValueError.
                     DiscogsAPIError)


class DiscogsPlugin(BeetsPlugin):

    def __init__(self):
        super(DiscogsPlugin, self).__init__()
        self.config.add({
            'apikey': 'rAzVUQYRaoFjeBjyWuWZ',
            'apisecret': 'plxtUTqoCzwxZpqdPysCwGuBSmZNdZVy',
            'tokenfile': 'discogs_token.json',
            'source_weight': 0.5,
        })
        self.config['apikey'].redact = True
        self.config['apisecret'].redact = True
        self.discogs_client = None
        self.register_listener('import_begin', self.setup)

    def setup(self, session=None):
        """Create the `discogs_client` field. Authenticate if necessary.
        """
        c_key = self.config['apikey'].get(unicode)
        c_secret = self.config['apisecret'].get(unicode)

        # Get the OAuth token from a file or log in.
        try:
            with open(self._tokenfile()) as f:
                tokendata = json.load(f)
        except IOError:
            # No token yet. Generate one.
            token, secret = self.authenticate(c_key, c_secret)
        else:
            token = tokendata['token']
            secret = tokendata['secret']

        self.discogs_client = Client(USER_AGENT, c_key, c_secret,
                                     token, secret)

    def reset_auth(self):
        """Delete toke file & redo the auth steps.
        """
        os.remove(self._tokenfile())
        self.setup()

    def _tokenfile(self):
        """Get the path to the JSON file for storing the OAuth token.
        """
        return self.config['tokenfile'].get(confit.Filename(in_app_dir=True))

    def authenticate(self, c_key, c_secret):
        # Get the link for the OAuth page.
        auth_client = Client(USER_AGENT, c_key, c_secret)
        try:
            _, _, url = auth_client.get_authorize_url()
        except CONNECTION_ERRORS as e:
            self._log.debug('connection error: {0}', e)
            raise beets.ui.UserError('communication with Discogs failed')

        beets.ui.print_("To authenticate with Discogs, visit:")
        beets.ui.print_(url)

        # Ask for the code and validate it.
        code = beets.ui.input_("Enter the code:")
        try:
            token, secret = auth_client.get_access_token(code)
        except DiscogsAPIError:
            raise beets.ui.UserError('Discogs authorization failed')
        except CONNECTION_ERRORS as e:
            self._log.debug(u'connection error: {0}', e)
            raise beets.ui.UserError('Discogs token request failed')

        # Save the token for later use.
        self._log.debug('Discogs token {0}, secret {1}', token, secret)
        with open(self._tokenfile(), 'w') as f:
            json.dump({'token': token, 'secret': secret}, f)

        return token, secret

    def album_distance(self, items, album_info, mapping):
        """Returns the album distance.
        """
        dist = Distance()
        if album_info.data_source == 'Discogs':
            dist.add('source', self.config['source_weight'].as_number())
        return dist

    def candidates(self, items, artist, album, va_likely):
        """Returns a list of AlbumInfo objects for discogs search results
        matching an album and artist (if not various).
        """
        if not self.discogs_client:
            return

        if va_likely:
            query = album
        else:
            query = '%s %s' % (artist, album)
        try:
            return self.get_albums(query)
        except DiscogsAPIError as e:
            self._log.debug(u'API Error: {0} (query: {1})', e, query)
            if e.status_code == 401:
                self.reset_auth()
                return self.candidates(items, artist, album, va_likely)
            else:
                return []
        except CONNECTION_ERRORS:
            self._log.debug('Connection error in album search', exc_info=True)
            return []

    def album_for_id(self, album_id):
        """Fetches an album by its Discogs ID and returns an AlbumInfo object
        or None if the album is not found.
        """
        if not self.discogs_client:
            return

        self._log.debug(u'Searching for release {0}', album_id)
        # Discogs-IDs are simple integers. We only look for those at the end
        # of an input string as to avoid confusion with other metadata plugins.
        # An optional bracket can follow the integer, as this is how discogs
        # displays the release ID on its webpage.
        match = re.search(r'(^|\[*r|discogs\.com/.+/release/)(\d+)($|\])',
                          album_id)
        if not match:
            return None
        result = Release(self.discogs_client, {'id': int(match.group(2))})
        # Try to obtain title to verify that we indeed have a valid Release
        try:
            getattr(result, 'title')
        except DiscogsAPIError as e:
            if e.status_code != 404:
                self._log.debug(u'API Error: {0} (query: {1})', e, result._uri)
                if e.status_code == 401:
                    self.reset_auth()
                    return self.album_for_id(album_id)
            return None
        except CONNECTION_ERRORS:
            self._log.debug('Connection error in album lookup', exc_info=True)
            return None
        return self.get_album_info(result)

    def get_albums(self, query):
        """Returns a list of AlbumInfo objects for a discogs search query.
        """
        # Strip non-word characters from query. Things like "!" and "-" can
        # cause a query to return no results, even if they match the artist or
        # album title. Use `re.UNICODE` flag to avoid stripping non-english
        # word characters.
        # TEMPORARY: Encode as ASCII to work around a bug:
        # https://github.com/sampsyo/beets/issues/1051
        # When the library is fixed, we should encode as UTF-8.
        query = re.sub(r'(?u)\W+', ' ', query).encode('ascii', "replace")
        # Strip medium information from query, Things like "CD1" and "disk 1"
        # can also negate an otherwise positive result.
        query = re.sub(r'(?i)\b(CD|disc)\s*\d+', '', query)
        try:
            releases = self.discogs_client.search(query,
                                                  type='release').page(1)
        except CONNECTION_ERRORS:
            self._log.debug("Communication error while searching for {0!r}",
                            query, exc_info=True)
            return []
        return [self.get_album_info(release) for release in releases[:5]]

    def get_album_info(self, result):
        """Returns an AlbumInfo object for a discogs Release object.
        """
        artist, artist_id = self.get_artist([a.data for a in result.artists])
        album = re.sub(r' +', ' ', result.title)
        album_id = result.data['id']
        # Use `.data` to access the tracklist directly instead of the
        # convenient `.tracklist` property, which will strip out useful artist
        # information and leave us with skeleton `Artist` objects that will
        # each make an API call just to get the same data back.
        tracks = self.get_tracks(result.data['tracklist'])
        albumtype = ', '.join(
            result.data['formats'][0].get('descriptions', [])) or None
        va = result.data['artists'][0]['name'].lower() == 'various'
        if va:
            artist = config['va_name'].get(unicode)
        year = result.data['year']
        label = result.data['labels'][0]['name']
        mediums = len(set(t.medium for t in tracks))
        catalogno = result.data['labels'][0]['catno']
        if catalogno == 'none':
            catalogno = None
        country = result.data.get('country')
        media = result.data['formats'][0]['name']
        data_url = result.data['uri']
        return AlbumInfo(album, album_id, artist, artist_id, tracks, asin=None,
                         albumtype=albumtype, va=va, year=year, month=None,
                         day=None, label=label, mediums=mediums,
                         artist_sort=None, releasegroup_id=None,
                         catalognum=catalogno, script=None, language=None,
                         country=country, albumstatus=None, media=media,
                         albumdisambig=None, artist_credit=None,
                         original_year=None, original_month=None,
                         original_day=None, data_source='Discogs',
                         data_url=data_url)

    def get_artist(self, artists):
        """Returns an artist string (all artists) and an artist_id (the main
        artist) for a list of discogs album or track artists.
        """
        artist_id = None
        bits = []
        for i, artist in enumerate(artists):
            if not artist_id:
                artist_id = artist['id']
            name = artist['name']
            # Strip disambiguation number.
            name = re.sub(r' \(\d+\)$', '', name)
            # Move articles to the front.
            name = re.sub(r'(?i)^(.*?), (a|an|the)$', r'\2 \1', name)
            bits.append(name)
            if artist['join'] and i < len(artists) - 1:
                bits.append(artist['join'])
        artist = ' '.join(bits).replace(' ,', ',') or None
        return artist, artist_id

    def get_tracks(self, tracklist):
        """Returns a list of TrackInfo objects for a discogs tracklist.
        """
        tracks = []
        index_tracks = {}
        index = 0
        for track in tracklist:
            # Only real tracks have `position`. Otherwise, it's an index track.
            if track['position']:
                index += 1
                tracks.append(self.get_track_info(track, index))
            else:
                index_tracks[index + 1] = track['title']

        # Fix up medium and medium_index for each track. Discogs position is
        # unreliable, but tracks are in order.
        medium = None
        medium_count, index_count = 0, 0
        for track in tracks:
            # Handle special case where a different medium does not indicate a
            # new disc, when there is no medium_index and the ordinal of medium
            # is not sequential. For example, I, II, III, IV, V. Assume these
            # are the track index, not the medium.
            medium_is_index = track.medium and not track.medium_index and (
                len(track.medium) != 1 or
                ord(track.medium) - 64 != medium_count + 1
            )

            if not medium_is_index and medium != track.medium:
                # Increment medium_count and reset index_count when medium
                # changes.
                medium = track.medium
                medium_count += 1
                index_count = 0
            index_count += 1
            track.medium, track.medium_index = medium_count, index_count

        # Get `disctitle` from Discogs index tracks. Assume that an index track
        # before the first track of each medium is a disc title.
        for track in tracks:
            if track.medium_index == 1:
                if track.index in index_tracks:
                    disctitle = index_tracks[track.index]
                else:
                    disctitle = None
            track.disctitle = disctitle

        return tracks

    def get_track_info(self, track, index):
        """Returns a TrackInfo object for a discogs track.
        """
        title = track['title']
        track_id = None
        medium, medium_index = self.get_track_index(track['position'])
        artist, artist_id = self.get_artist(track.get('artists', []))
        length = self.get_track_length(track['duration'])
        return TrackInfo(title, track_id, artist, artist_id, length, index,
                         medium, medium_index, artist_sort=None,
                         disctitle=None, artist_credit=None)

    def get_track_index(self, position):
        """Returns the medium and medium index for a discogs track position.
        """
        # medium_index is a number at the end of position. medium is everything
        # else. E.g. (A)(1), (Side A, Track )(1), (A)(), ()(1), etc.
        match = re.match(r'^(.*?)(\d*)$', position.upper())
        if match:
            medium, index = match.groups()
        else:
            self._log.debug(u'Invalid position: {0}', position)
            medium = index = None
        return medium or None, index or None

    def get_track_length(self, duration):
        """Returns the track length in seconds for a discogs duration.
        """
        try:
            length = time.strptime(duration, '%M:%S')
        except ValueError:
            return None
        return length.tm_min * 60 + length.tm_sec
