#! /usr/bin/python

# This is required to work around the ImportError exception
# "Failed to import _strptime because the import lock is held by another thread."
import _strptime

import time
import re
import os
import urlparse
from datetime import datetime
from collections import OrderedDict
from urllib2 import unquote

from bs4 import BeautifulSoup, SoupStrainer
import requests
import html2text
import json

import libreelec, funcs, log

from .addon import L10n


timeout = None
arch = libreelec.ARCH
date_fmt = '%d %b %y'

#Errors

class BuildURLError(Exception):
    pass

#Builds

class Build(object):
    DATETIME_FMT = '%Y%m%d%H%M%S'

    def __init__(self, _datetime, version):
        self._version = version
        self._is_release = False
        log.log("version - {}".format(version))
        if isinstance(_datetime, datetime):
            self._datetime = _datetime
        else:
            try:
                self._datetime = datetime.strptime(_datetime, self.DATETIME_FMT)
            except TypeError:
                dt = time.strptime(_datetime, self.DATETIME_FMT)[0:6]
                self._datetime = datetime(*(dt))

    def __eq__(self, other):
        return (self._version, self._datetime) == (other._version, other._datetime)

    def __hash__(self):
        return hash((self._version, self._datetime))

    def __lt__(self, other):
        return self._datetime < other._datetime

    def __gt__(self, other):
        return self._datetime > other._datetime

    def __str__(self):
        return '{} ({})'.format(self.version, self.date)

    def __repr__(self):
        return "{}('{}', '{}')".format("Build",
                                       self._datetime.strftime(self.DATETIME_FMT),
                                       self.version)

    @property
    def date(self):
        return self._datetime.strftime(date_fmt)

    @property
    def version(self):
        return self._version

    @property
    def is_release(self):
        return self._is_release


class Release(Build):
    DATETIME_FMT = '%Y-%m-%dT%H:%M:%S'
    MIN_VERSION = [6,90,0]

    tags = None

    def __init__(self, version):
        self.release_str = version
        log.log("version - {}".format(version))
        Build.__init__(self, '2017-02-21T21:11:00', version)
        self._is_release = True
        self.release = [int(p) for p in version.split('.')]
        log.log("release - {}".format(self.release))

    def is_valid(self):
        return self.release >= self.MIN_VERSION

    __nonzero__ = is_valid

    def __lt__(self, other):
        return self.version < other.version

    def __gt__(self, other):
        return self.version > other.version

    def __repr__(self):
        return "{}('{}')".format("Release", self.release_str)


class BuildLinkBase(object):
    def __init__(self, baseurl, link):
        # Set the absolute URL
        link = link.strip()
        log.log("link - {}".format(link))
        scheme, netloc, path = urlparse.urlparse(link)[:3]
        if not scheme:
            # Construct the full url
            if not baseurl.endswith('/'):
                baseurl += '/'
            self.url = urlparse.urljoin(baseurl, link)
        else:
            if netloc == "www.dropbox.com":
                # Fix Dropbox url
                link = urlparse.urlunparse((scheme, "dl.dropbox.com", path,
                                            None, None, None))
            self.url = link
        log.log("url - {}".format(self.url))

    def remote_file(self):
        response = requests.get(self.url, stream=True, timeout=timeout,
                                headers={'Accept-Encoding': None})
        try:
            self.size = int(response.headers['Content-Length'])
        except KeyError:
            log.log("Header key Content-Length not found")
            self.size = 0

        # Get the actual filename
        self.filename = unquote(os.path.basename(urlparse.urlparse(response.url).path))

        # Fix filename
        try:
            newfilename = re.findall("filename=(.+)", response.headers['Content-Disposition'])[0]
            self.filename = newfilename
        except KeyError:
            log.log("Header key Content-Disposition not found")

        name, ext = os.path.splitext(self.filename)
        self.tar_name = self.filename if ext == '.tar' else name
        self.compressed = ext == '.bz2'

        return response.raw


class BuildLink(Build, BuildLinkBase):
    def __init__(self, baseurl, link, datetime_str, revision):
        BuildLinkBase.__init__(self, baseurl, link)
        Build.__init__(self, datetime_str, version=revision)


class ReleaseLink(Release, BuildLinkBase):
    def __init__(self, baseurl, link, release):
        BuildLinkBase.__init__(self, baseurl, link)
        Release.__init__(self, release)

#Extractors

class BaseExtractor(object):
    url = None

    def __init__(self, url=None):
        if url is not None:
            self.url = url

    def _response(self):
        response = requests.get(self.url, timeout=timeout)
        if not response:
            msg = "Build URL error: status {}".format(response.status_code)
            raise BuildURLError(msg)
        return response

    def _text(self):
        return self._response().text

    def _json(self):
        return self._response().json()

    def __repr__(self):
        return "{}('{}')".format(self.__class__.__name__, self.url)


class BuildLinkExtractor(BaseExtractor):
    BUILD_RE = (".*{dist}.*-{arch}-(?:\d+\.\d+-|)[a-zA-Z]+-(\d+)"
                "-r\d+[a-z]*-g([0-9a-z]+)\.tar(|\.bz2)")
    CSS_CLASS = None

    def __iter__(self):
        html = self._text()
        args = ['a']
        if self.CSS_CLASS is not None:
            args.append(self.CSS_CLASS)

        self.build_re = re.compile(self.BUILD_RE.format(dist=libreelec.dist(), arch=arch), re.I)

        soup = BeautifulSoup(html, 'html.parser',
                             parse_only=SoupStrainer(*args, href=self.build_re))

        for link in soup.contents:
            l = self._create_link(link)
            if l:
                yield l

    def _create_link(self, link):
        href = link['href']
        log.log("href - {}".format(href))
        log.log("match - {}".format(*self.build_re.match(href).groups()[:2]))
        return BuildLink(self.url, href, *self.build_re.match(href).groups()[:2])

class BuildLinkExtractorMultiPage(BuildLinkExtractor):

    def __iter__(self):
        html = self._text()
        args = ['a']
        if self.CSS_CLASS is not None:
            args.append(self.CSS_CLASS)

        self.build_re = re.compile(self.BUILD_RE.format(dist=libreelec.dist(), arch=arch), re.I)

        while True:

            soup = BeautifulSoup(html, 'html.parser',
                             parse_only=SoupStrainer(*args, href=self.build_re))

            for link in soup.contents:
                l = self._create_link(link)
                if l:
                    yield l

            soup_next = BeautifulSoup(html, 'html.parser',
                                     parse_only=SoupStrainer('div', {'class': 'pagination'}))
            next_page_link = soup_next.find('a', text='Next')

            if next_page_link:

                href = next_page_link['href']
                html = requests.get(href).text

            else:
                break

class DropboxBuildLinkExtractor(BuildLinkExtractor):
    CSS_CLASS = 'filename-link'

class YDBuildLinkExtractor(BuildLinkExtractor):
    BUILD_RE = ".*{dist}.*-{arch}-[\d\.]+-(\d+)-r\d+[a-z]*-g([0-9a-z]+)\.tar(|\.bz2)"

class YDBuildLinkExtractorAll(BuildLinkExtractorMultiPage):
    BUILD_RE = ".*{dist}.*-{arch}-[\d\.]+-(\d+)-r\d+[a-z]*-g([0-9a-z]+)\.tar(|\.bz2)"

class ReleaseLinkExtractor(BuildLinkExtractor):
    BUILD_RE = ".*{dist}.*-{arch}-([\d\.]+)\.tar(|\.bz2)"
    BASE_URL = None

    def _create_link(self, link):
        href = link['href']
        log.log("href - {}".format(href))
        log.log("match - {}".format(self.build_re.match(href).group(1)))
        return ReleaseLink(self.url, href, self.build_re.match(href).group(1))

#BuildsInfo

class BuildInfo(object):
    def __init__(self, summary, details=None):
        self.summary = summary
        self.details = details

    def __str__(self):
        return self.summary


class BuildDetailsExtractor(BaseExtractor):
    def get_text(self):
        return ""

class BuildInfoExtractor(BaseExtractor):
    def get_info(self):
        return {}

class CommitInfoExtractor(BuildInfoExtractor):
    url = "https://api.github.com/repositories/1093060/commits?per_page=100"

    def get_info(self):
        return dict((commit['sha'][:7],
                     BuildInfo(commit['commit']['message'].split('\n\n')[0], None))
                     for commit in self._json())

#BuildsURL

class BuildsURL(object):
    def __init__(self, url, subdir=None, extractor=BuildLinkExtractor,
                 info_extractors=[BuildInfoExtractor()]):
        self.url = url
        if subdir:
            self.add_subdir(subdir)

        self._extractor = extractor
        self.info_extractors = info_extractors

    def builds(self):
        return sorted(self._extractor(self.url), reverse=True)

    def __iter__(self):
        return iter(self.builds())

    def latest(self):
        builds = self.builds()
        try:
            return builds[0]
        except IndexError:
            return None

    def add_subdir(self, subdir):
        self._add_slash()
        self.url = urlparse.urljoin(self.url, subdir)
        self._add_slash()

    def _add_slash(self):
        if not self.url.endswith('/'):
            self.url += '/'

    def __str__(self):
        return self.url

    def __repr__(self):
        return "{}('{}')".format(self.__class__.__name__, self.url)


def get_installed_build():
    DEVEL_RE = ".*-(\d+)-r\d+-g([a-z0-9]+)"

    if libreelec.OS_RELEASE['NAME'] in ("LibreELEC"):
        version = libreelec.OS_RELEASE['VERSION']
    else:
        # For testing on a non OpenELEC machine
        version = 'devel-20150503135721-r20764-gbfd3782'

    m = re.match(DEVEL_RE, version)
    if m:
        return Build(*m.groups())
    else:
        # A full release is installed.
        return Release(version)


def sources():
    _sources = OrderedDict()

    _sources["YLLOW_DRAGON"] = BuildsURL(
        "https://github.com/nkvoronov/{dist}.tv/releases".format(dist=libreelec.OS_RELEASE['NAME']),
        extractor=YDBuildLinkExtractorAll)

    _sources["LibreELEC.tv"] = BuildsURL(
        "http://archive.{dist}.tv".format(dist=libreelec.dist()),
        extractor=ReleaseLinkExtractor)

    return _sources


def latest_build(source):
    build_sources = sources()
    try:
        build_url = build_sources[source]
    except KeyError:
        return None
    else:
        return build_url.latest()


@log.with_logging(msg_error="Unable to create build object from the notify file")
def get_build_from_notify_file():
    selected = funcs.read_notify_file()
    if selected:
        source, build_repr = selected
        return source, eval(build_repr)


def main():
    import sys

    installed_build = get_installed_build()

    def get_info(build_url):
        info = {}
        for info_extractor in build_url.info_extractors:
            try:
                info.update(info_extractor.get_info())
            except Exception as e:
                print str(e)
        return info

    def print_links(name, build_url):
        info = get_info(build_url)
        print name
        try:
            for link in build_url:
                try:
                    summary = info[link.version]
                except KeyError:
                    summary = ""
                print "\t{:25s} {}".format(str(link) + ' *' * (link > installed_build),
                                           summary)
        except (requests.RequestException, BuildURLError) as e:
            print str(e)
        print

    print "Installed build = {}".format(installed_build)
    print

    urls = sources()

    if len(sys.argv) > 1:
        name = sys.argv[1]
        if name not in urls:
            print '"{}" not in URL list'.format(name)
        else:
            print_links(name, urls[name])
    else:
        for name, build_url in urls.items():
            print_links(name, build_url)


if __name__ == "__main__":
    main()
