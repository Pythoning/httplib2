from __future__ import generators
"""
httplib2

A caching http interface that supports ETags and gzip
to conserve bandwidth. 

Requires Python 2.3 or later

"""

__author__ = "Joe Gregorio (joe@bitworking.org)"
__copyright__ = "Copyright 2006, Joe Gregorio"
__contributors__ = ["Thomas Broyer (t.broyer@ltgt.net)",
    "James Antill",
    "Xavier Verges Farrero",
    "Jonathan Feinberg",
    "Blair Zajac",
    "Sam Ruby"]
__license__ = "MIT"
__version__ = "$Rev$"

import re 
import md5
import email
import email.Utils
import email.Message
import StringIO
import gzip
import zlib
import httplib
import urlparse
import base64
import os
import copy
import calendar
import time
import random
import sha
import hmac
from gettext import gettext as _
from socket import gaierror

__all__ = ['Http', 'Response', 'HttpLib2Error',
  'RedirectMissingLocation', 'RedirectLimit', 'FailedToDecompressContent', 
  'UnimplementedDigestAuthOptionError', 'UnimplementedHmacDigestAuthOptionError',
  'debuglevel']


# The httplib debug level, set to a non-zero value to get debug output
debuglevel = 0

# Python 2.3 support
if 'sorted' not in __builtins__:
    def sorted(seq):
        seq.sort()
        return seq

# Python 2.3 support
def HTTPResponse__getheaders(self):
    """Return list of (header, value) tuples."""
    if self.msg is None:
        print "================================"
        raise httplib.ResponseNotReady()
    return self.msg.items()

if not hasattr(httplib.HTTPResponse, 'getheaders'):
    httplib.HTTPResponse.getheaders = HTTPResponse__getheaders

# All exceptions raised here derive from HttpLib2Error
class HttpLib2Error(Exception): pass

class RedirectMissingLocation(HttpLib2Error): pass
class RedirectLimit(HttpLib2Error): pass
class FailedToDecompressContent(HttpLib2Error): pass
class UnimplementedDigestAuthOptionError(HttpLib2Error): pass
class UnimplementedHmacDigestAuthOptionError(HttpLib2Error): pass

# Open Items:
# -----------
# Proxy support

# Are we removing the cached content too soon on PUT (only delete on 200 Maybe?)

# Pluggable cache storage (supports storing the cache in
#   flat files by default. We need a plug-in architecture
#   that can support Berkeley DB and Squid)

# == Known Issues ==
# Does not handle a resource that uses conneg and Last-Modified but no ETag as a cache validator.
# Does not handle Cache-Control: max-stale
# Does not use Age: headers when calculating cache freshness.


# The number of redirections to follow before giving up.
# Note that only GET redirects are automatically followed.
# Will also honor 301 requests by saving that info and never
# requesting that URI again.
DEFAULT_MAX_REDIRECTS = 5

# Which headers are hop-by-hop headers by default
HOP_BY_HOP = ['connection', 'keep-alive', 'proxy-authenticate', 'proxy-authorization', 'te', 'trailers', 'transfer-encoding', 'upgrade']

def _get_end2end_headers(response):
    hopbyhop = list(HOP_BY_HOP)
    hopbyhop.extend([x.strip() for x in response.get('connection', '').split(',')])
    return [header for header in response.keys() if header not in hopbyhop]

URI = re.compile(r"^(([^:/?#]+):)?(//([^/?#]*))?([^?#]*)(\?([^#]*))?(#(.*))?")

def parse_uri(uri):
    """Parses a URI using the regex given in Appendix B of RFC 3986.

        (scheme, authority, path, query, fragment) = parse_uri(uri)
    """
    groups = URI.match(uri).groups()
    return (groups[1], groups[3], groups[4], groups[6], groups[8])

def urlnorm(uri):
    (scheme, authority, path, query, fragment) = parse_uri(uri)
    authority = authority.lower()
    if not path: 
        path = "/"
    # Could do syntax based normalization of the URI before
    # computing the digest. See Section 6.2.2 of Std 66.
    request_uri = query and "?".join([path, query]) or path
    defrag_uri = scheme + "://" + authority + request_uri
    return scheme, authority, request_uri, defrag_uri


# Cache filename construction (original borrowed from Venus http://intertwingly.net/code/venus/)
re_url_scheme    = re.compile(r'^\w+://')
re_slash         = re.compile(r'[?/:|]+')

def safename(filename):
    """Return a filename suitable for the cache.

    Strips dangerous and common characters to create a filename we
    can use to store the cache in.
    """

    try:
        if re_url_scheme.match(filename):
            if isinstance(filename,str):
                filename=filename.decode('utf-8').encode('idna')
            else:
                filename=filename.encode('idna')
    except:
        pass
    if isinstance(filename,unicode):
        filename=filename.encode('utf-8')
    filemd5 = md5.new(filename).hexdigest()
    filename = re_url_scheme.sub("", filename)
    filename = re_slash.sub(",", filename)

    # limit length of filename
    if len(filename)>200:
        filename=filename[:200]
    return ",".join((filename, filemd5))

NORMALIZE_SPACE = re.compile(r'(?:\r\n)?[ \t]+')
def _normalize_headers(headers):
    return dict([ (key.lower(), NORMALIZE_SPACE.sub(value, ' ').strip())  for (key, value) in headers.iteritems()])

def _parse_cache_control(headers):
    retval = {}
    if headers.has_key('cache-control'):
        parts =  headers['cache-control'].split(',')
        parts_with_args = [tuple([x.strip() for x in part.split("=")]) for part in parts if -1 != part.find("=")]
        parts_wo_args = [(name.strip(), 1) for name in parts if -1 == name.find("=")]
        retval = dict(parts_with_args + parts_wo_args)
    return retval 

# Whether to use a strict mode to parse WWW-Authenticate headers
# Might lead to bad results in case of ill-formed header value,
# so disabled by default, falling back to relaxed parsing.
# Set to true to turn on, usefull for testing servers.
USE_WWW_AUTH_STRICT_PARSING = 0

# In regex below:
#    [^\0-\x1f\x7f-\xff()<>@,;:\\\"/[\]?={} \t]+             matches a "token" as defined by HTTP
#    "(?:[^\0-\x08\x0A-\x1f\x7f-\xff\\\"]|\\[\0-\x7f])*?"    matches a "quoted-string" as defined by HTTP, when LWS have already been replaced by a single space
# Actually, as an auth-param value can be either a token or a quoted-string, they are combined in a single pattern which matches both:
#    \"?((?<=\")(?:[^\0-\x1f\x7f-\xff\\\"]|\\[\0-\x7f])*?(?=\")|(?<!\")[^\0-\x08\x0A-\x1f\x7f-\xff()<>@,;:\\\"/[\]?={} \t]+(?!\"))\"?
WWW_AUTH_STRICT = re.compile(r"^(?:\s*(?:,\s*)?([^\0-\x1f\x7f-\xff()<>@,;:\\\"/[\]?={} \t]+)\s*=\s*\"?((?<=\")(?:[^\0-\x08\x0A-\x1f\x7f-\xff\\\"]|\\[\0-\x7f])*?(?=\")|(?<!\")[^\0-\x1f\x7f-\xff()<>@,;:\\\"/[\]?={} \t]+(?!\"))\"?)(.*)$")
WWW_AUTH_RELAXED = re.compile(r"^(?:\s*(?:,\s*)?([^ \t\r\n=]+)\s*=\s*\"?((?<=\")(?:[^\\\"]|\\.)*?(?=\")|(?<!\")[^ \t\r\n,]+(?!\"))\"?)(.*)$")
UNQUOTE_PAIRS = re.compile(r'\\(.)')
def _parse_www_authenticate(headers, headername='www-authenticate'):
    """Returns a dictionary of dictionaries, one dict
    per auth_scheme."""
    retval = {}
    if headers.has_key(headername):
        authenticate = headers[headername].strip()
        www_auth = USE_WWW_AUTH_STRICT_PARSING and WWW_AUTH_STRICT or WWW_AUTH_RELAXED
        while authenticate:
            # Break off the scheme at the beginning of the line
            if headername == 'authentication-info':
                (auth_scheme, the_rest) = ('digest', authenticate)                
            else:
                (auth_scheme, the_rest) = authenticate.split(" ", 1)
            # Now loop over all the key value pairs that come after the scheme, 
            # being careful not to roll into the next scheme
            match = www_auth.search(the_rest)
            auth_params = {}
            while match:
                if match and len(match.groups()) == 3:
                    (key, value, the_rest) = match.groups()
                    auth_params[key.lower()] = UNQUOTE_PAIRS.sub(r'\1', value) # '\\'.join([x.replace('\\', '') for x in value.split('\\\\')])
                match = www_auth.search(the_rest)
            retval[auth_scheme.lower()] = auth_params
            authenticate = the_rest.strip()
    return retval


def _entry_disposition(response_headers, request_headers):
    """Determine freshness from the Date, Expires and Cache-Control headers.

    We don't handle the following:

    1. Cache-Control: max-stale
    2. Age: headers are not used in the calculations.

    Not that this algorithm is simpler than you might think 
    because we are operating as a private (non-shared) cache.
    This lets us ignore 's-maxage'. We can also ignore
    'proxy-invalidate' since we aren't a proxy.
    We will never return a stale document as 
    fresh as a design decision, and thus the non-implementation 
    of 'max-stale'. This also lets us safely ignore 'must-revalidate' 
    since we operate as if every server has sent 'must-revalidate'.
    Since we are private we get to ignore both 'public' and
    'private' parameters. We also ignore 'no-transform' since
    we don't do any transformations.    
    The 'no-store' parameter is handled at a higher level.
    So the only Cache-Control parameters we look at are:

    no-cache
    only-if-cached
    max-age
    min-fresh
    """
    
    retval = "STALE"
    cc = _parse_cache_control(request_headers)
    cc_response = _parse_cache_control(response_headers)

    if request_headers.has_key('pragma') and request_headers['pragma'].lower().find('no-cache') != -1:
        retval = "TRANSPARENT"
        if 'cache-control' not in request_headers:
            request_headers['cache-control'] = 'no-cache'
    elif cc.has_key('no-cache'):
        retval = "TRANSPARENT"
    elif cc_response.has_key('no-cache'):
        retval = "STALE"
    elif cc.has_key('only-if-cached'):
        retval = "FRESH"
    elif response_headers.has_key('date'):
        date = calendar.timegm(email.Utils.parsedate_tz(response_headers['date']))
        now = time.time()
        current_age = max(0, now - date)
        if cc_response.has_key('max-age'):
            freshness_lifetime = int(cc_response['max-age'])
        elif response_headers.has_key('expires'):
            expires = email.Utils.parsedate_tz(response_headers['expires'])
            freshness_lifetime = max(0, calendar.timegm(expires) - date)
        else:
            freshness_lifetime = 0
        if cc.has_key('max-age'):
            freshness_lifetime = min(freshness_lifetime, int(cc['max-age']))
        if cc.has_key('min-fresh'):
            current_age += int(cc['min-fresh'])
        if freshness_lifetime > current_age:
            retval = "FRESH"
    return retval 

def _decompressContent(response, new_content):
    content = new_content
    try:
        if response.get('content-encoding', None) == 'gzip':
            content = gzip.GzipFile(fileobj=StringIO.StringIO(new_content)).read()
            response['content-length'] = str(len(content))
        if response.get('content-encoding', None) == 'deflate':
            content = zlib.decompress(content)
            response['content-length'] = str(len(content))
    except:
        content = ""
        raise FailedToDecompressContent(_("Content purported to be compressed with %s but failed to decompress.") % response.get('content-encoding'))
    return content

def _updateCache(request_headers, response_headers, content, cache, cachekey):
    if cachekey:
        cc = _parse_cache_control(request_headers)
        cc_response = _parse_cache_control(response_headers)
        if cc.has_key('no-store') or cc_response.has_key('no-store'):
            cache.delete(cachekey)
        else:
            info = email.Message.Message()
            for key, value in response_headers.iteritems():
                info[key] = value
            text = info.as_string()
            #from cStringIO import StringIO
            #from email.Generator import Generator
            #fp = StringIO()
            #g = Generator(fp, mangle_from_=False, maxheaderlen=60)
            #g.flatten(info)
            #text = fp.getvalue()
            text = re.sub("\r(?!\n)|(?<!\r)\n", "\r\n", text)
            text += content

            cache.set(cachekey, text)

def _cnonce():
    dig = md5.new("%s:%s" % (time.ctime(), ["0123456789"[random.randrange(0, 9)] for i in range(20)])).hexdigest()
    return dig[:16]

def _wsse_username_token(cnonce, iso_now, password):
    return base64.encodestring(sha.new("%s%s%s" % (cnonce, iso_now, password)).digest()).strip()


# For credentials we need two things, first 
# a pool of credential to try (not necesarily tied to BAsic, Digest, etc.)
# Then we also need a list of URIs that have already demanded authentication
# That list is tricky since sub-URIs can take the same auth, or the 
# auth scheme may change as you descend the tree.
# So we also need each Auth instance to be able to tell us
# how close to the 'top' it is.

class Authentication:
    def __init__(self, credentials, host, request_uri, headers, response, content, http):
        (scheme, authority, path, query, fragment) = parse_uri(request_uri)
        self.path = path
        self.host = host
        self.credentials = credentials
        self.http = http

    def depth(self, request_uri):
        (scheme, authority, path, query, fragment) = parse_uri(request_uri)
        return request_uri[len(self.path):].count("/")

    def inscope(self, host, request_uri):
        # XXX Should we normalize the request_uri?
        (scheme, authority, path, query, fragment) = parse_uri(request_uri)
        return (host == self.host) and path.startswith(self.path)

    def request(self, method, request_uri, headers, content):
        """Modify the request headers to add the appropriate
        Authorization header. Over-rise this in sub-classes."""
        pass

    def response(self, response, content):
        """Gives us a chance to update with new nonces
        or such returned from the last authorized response.
        Over-rise this in sub-classes if necessary.

        Return TRUE is the request is to be retried, for 
        example Digest may return stale=true.
        """
        return False



class BasicAuthentication(Authentication):
    def __init__(self, credentials, host, request_uri, headers, response, content, http):
        Authentication.__init__(self, credentials, host, request_uri, headers, response, content, http)

    def request(self, method, request_uri, headers, content):
        """Modify the request headers to add the appropriate
        Authorization header."""
        headers['authorization'] = 'Basic ' + base64.encodestring("%s:%s" % self.credentials).strip()  


class DigestAuthentication(Authentication):
    """Only do qop='auth' and MD5, since that 
    is all Apache currently implements"""
    def __init__(self, credentials, host, request_uri, headers, response, content, http):
        Authentication.__init__(self, credentials, host, request_uri, headers, response, content, http)
        challenge = _parse_www_authenticate(response, 'www-authenticate')
        self.challenge = challenge['digest']
        qop = self.challenge.get('qop')
        self.challenge['qop'] = ('auth' in [x.strip() for x in qop.split()]) and 'auth' or None
        if self.challenge['qop'] is None:
            raise UnimplementedDigestAuthOptionError( _("Unsupported value for qop: %s." % qop))
        self.challenge['algorithm'] = self.challenge.get('algorithm', 'MD5')
        if self.challenge['algorithm'] != 'MD5':
            raise UnimplementedDigestAuthOptionError( _("Unsupported value for algorithm: %s." % self.challenge['algorithm']))
        self.A1 = "".join([self.credentials[0], ":", self.challenge['realm'], ":", self.credentials[1]])   
        self.challenge['nc'] = 1

    def request(self, method, request_uri, headers, content, cnonce = None):
        """Modify the request headers"""
        H = lambda x: md5.new(x).hexdigest()
        KD = lambda s, d: H("%s:%s" % (s, d))
        A2 = "".join([method, ":", request_uri])
        self.challenge['cnonce'] = cnonce or _cnonce() 
        request_digest  = '"%s"' % KD(H(self.A1), "%s:%s:%s:%s:%s" % (self.challenge['nonce'], 
                    '%08x' % self.challenge['nc'], 
                    self.challenge['cnonce'], 
                    self.challenge['qop'], H(A2)
                    )) 
        headers['Authorization'] = 'Digest username="%s", realm="%s", nonce="%s", uri="%s", algorithm=%s, response=%s, qop=%s, nc=%08x, cnonce="%s"' % (
                self.credentials[0], 
                self.challenge['realm'],
                self.challenge['nonce'],
                request_uri, 
                self.challenge['algorithm'],
                request_digest,
                self.challenge['qop'],
                self.challenge['nc'],
                self.challenge['cnonce'],
                )
        self.challenge['nc'] += 1

    def response(self, response, content):
        if not response.has_key('authentication-info'):
            challenge = _parse_www_authenticate(response, 'www-authenticate')['digest']
            if 'true' == challenge.get('stale'):
                self.challenge['nonce'] = challenge['nonce']
                self.challenge['nc'] = 1 
                return True
        else:
            updated_challenge = _parse_www_authenticate(response, 'authentication-info')['digest']

            if updated_challenge.has_key('nextnonce'):
                self.challenge['nonce'] = updated_challenge['nextnonce']
                self.challenge['nc'] = 1 
        return False


class HmacDigestAuthentication(Authentication):
    """Adapted from Robert Sayre's code and DigestAuthentication above."""
    __author__ = "Thomas Broyer (t.broyer@ltgt.net)"

    def __init__(self, credentials, host, request_uri, headers, response, content, http):
        Authentication.__init__(self, credentials, host, request_uri, headers, response, content, http)
        challenge = _parse_www_authenticate(response, 'www-authenticate')
        self.challenge = challenge['hmacdigest']
        print self.challenge
        # TODO: self.challenge['domain']
        self.challenge['reason'] = self.challenge.get('reason', 'unauthorized')
        if self.challenge['reason'] not in ['unauthorized', 'integrity']:
            self.challenge['reason'] = 'unauthorized'
        self.challenge['salt'] = self.challenge.get('salt', '')
        if not self.challenge.get('snonce'):
            raise UnimplementedHmacDigestAuthOptionError( _("The challenge doesn't contain a server nonce, or this one is empty."))
        self.challenge['algorithm'] = self.challenge.get('algorithm', 'HMAC-SHA-1')
        if self.challenge['algorithm'] not in ['HMAC-SHA-1', 'HMAC-MD5']:
            raise UnimplementedHmacDigestAuthOptionError( _("Unsupported value for algorithm: %s." % self.challenge['algorithm']))
        self.challenge['pw-algorithm'] = self.challenge.get('pw-algorithm', 'SHA-1')
        if self.challenge['pw-algorithm'] not in ['SHA-1', 'MD5']:
            raise UnimplementedHmacDigestAuthOptionError( _("Unsupported value for pw-algorithm: %s." % self.challenge['pw-algorithm']))
        if self.challenge['algorithm'] == 'HMAC-MD5':
            self.hashmod = md5
        else:
            self.hashmod = sha
        if self.challenge['pw-algorithm'] == 'MD5':
            self.pwhashmod = md5
        else:
            self.pwhashmod = sha
        self.key = "".join([self.credentials[0], ":",
                    self.pwhashmod.new("".join([self.credentials[1], self.challenge['salt']])).hexdigest().lower(),
                    ":", self.challenge['realm']
                    ])
        print response['www-authenticate']
        print "".join([self.credentials[1], self.challenge['salt']])
        print "key_str = %s" % self.key
        self.key = self.pwhashmod.new(self.key).hexdigest().lower()

    def request(self, method, request_uri, headers, content):
        """Modify the request headers"""
        keys = _get_end2end_headers(headers)
        keylist = "".join(["%s " % k for k in keys])
        headers_val = "".join([headers[k] for k in keys])
        created = time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime())
        cnonce = _cnonce()
        request_digest = "%s:%s:%s:%s:%s" % (method, request_uri, cnonce, self.challenge['snonce'], headers_val)
        print "key = %s" % self.key
        print "msg = %s" % request_digest
        request_digest  = hmac.new(self.key, request_digest, self.hashmod).hexdigest().lower()
        headers['Authorization'] = 'HMACDigest username="%s", realm="%s", snonce="%s", cnonce="%s", uri="%s", created="%s", response="%s", headers="%s"' % (
                self.credentials[0], 
                self.challenge['realm'],
                self.challenge['snonce'],
                cnonce,
                request_uri, 
                created,
                request_digest,
                keylist,
                )

    def response(self, response, content):
        challenge = _parse_www_authenticate(response, 'www-authenticate').get('hmacdigest', {})
        if challenge.get('reason') in ['integrity', 'stale']:
            return True
        return False


class WsseAuthentication(Authentication):
    """This is thinly tested and should not be relied upon.
    At this time there isn't any third party server to test against.
    Blogger and TypePad implemented this algorithm at one point
    but Blogger has since switched to Basic over HTTPS and 
    TypePad has implemented it wrong, by never issuing a 401
    challenge but instead requiring your client to telepathically know that
    their endpoint is expecting WSSE profile="UsernameToken"."""
    def __init__(self, credentials, host, request_uri, headers, response, content, http):
        Authentication.__init__(self, credentials, host, request_uri, headers, response, content, http)

    def request(self, method, request_uri, headers, content):
        """Modify the request headers to add the appropriate
        Authorization header."""
        headers['Authorization'] = 'WSSE profile="UsernameToken"'
        iso_now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        cnonce = _cnonce()
        password_digest = _wsse_username_token(cnonce, iso_now, self.credentials[1])
        headers['X-WSSE'] = 'UsernameToken Username="%s", PasswordDigest="%s", Nonce="%s", Created="%s"' % (
                self.credentials[0],
                password_digest,
                cnonce,
                iso_now)

class GoogleLoginAuthentication(Authentication):
    def __init__(self, credentials, host, request_uri, headers, response, content, http):
        from urllib import urlencode
        Authentication.__init__(self, credentials, host, request_uri, headers, response, content, http)

        auth = dict(Email=credentials[0], Passwd=credentials[1], service='cl', source=headers['user-agent'])
        resp, content = self.http.request("https://www.google.com/accounts/ClientLogin", method="POST", body=urlencode(auth), headers={'Content-Type': 'application/x-www-form-urlencoded'})
        lines = content.split('\n')
        d = dict([tuple(line.split("=", 1)) for line in lines if line])
        if resp.status == 403:
            self.Auth = ""
        else:
            self.Auth = d['Auth']

    def request(self, method, request_uri, headers, content):
        """Modify the request headers to add the appropriate
        Authorization header."""
        headers['authorization'] = 'GoogleLogin Auth=' + self.Auth 


AUTH_SCHEME_CLASSES = {
    "basic": BasicAuthentication,
    "wsse": WsseAuthentication,
    "digest": DigestAuthentication,
    "hmacdigest": HmacDigestAuthentication,
    "googlelogin": GoogleLoginAuthentication
}

AUTH_SCHEME_ORDER = ["hmacdigest", "googlelogin", "digest", "wsse", "basic"]

def _md5(s):
    return 

class FileCache:
    """Uses a local directory as a store for cached files.
    Not really safe to use if multiple threads or processes are going to 
    be running on the same cache.
    """
    def __init__(self, cache, safe=safename): # use safe=lambda x: md5.new(x).hexdigest() for the old behavior
        self.cache = cache
        self.safe = safe
        if not os.path.exists(cache): 
            os.makedirs(self.cache)

    def get(self, key):
        retval = None
        cacheFullPath = os.path.join(self.cache, self.safe(key))
        try:
            f = file(cacheFullPath, "r")
            retval = f.read()
            f.close()
        except:
            pass
        return retval

    def set(self, key, value):
        cacheFullPath = os.path.join(self.cache, self.safe(key))
        f = file(cacheFullPath, "w")
        f.write(value)
        f.close()

    def delete(self, key):
        cacheFullPath = os.path.join(self.cache, self.safe(key))
        if os.path.exists(cacheFullPath):
            os.remove(cacheFullPath)

class Http:
    """An HTTP client that handles all 
    methods, caching, ETags, compression,
    HTTPS, Basic, Digest, WSSE, etc.
    """
    def __init__(self, cache=None):
        # Map domain name to an httplib connection
        self.connections = {}
        # The location of the cache, for now a directory
        # where cached responses are held.
        if cache and isinstance(cache, str):
            self.cache = FileCache(cache)
        else:
            self.cache = cache

        # tuples of name, password
        self.credentials = []

        # authorization objects
        self.authorizations = []

        self.follow_all_redirects = False

        self.ignore_etag = False

    def _auth_from_challenge(self, host, request_uri, headers, response, content):
        """A generator that creates Authorization objects
           that can be applied to requests.
        """
        challenges = _parse_www_authenticate(response, 'www-authenticate')
        for cred in self.credentials:
            for scheme in AUTH_SCHEME_ORDER:
                if challenges.has_key(scheme):
                    yield AUTH_SCHEME_CLASSES[scheme](cred, host, request_uri, headers, response, content, self) 

    def add_credentials(self, name, password):
        """Add a name and password that will be used
        any time a request requires authentication."""
        self.credentials.append((name, password))

    def clear_credentials(self):
        """Remove all the names and passwords
        that are used for authentication"""
        self.credentials = []
        self.authorizations = []

    def _conn_request(self, conn, request_uri, method, body, headers):
        for i in range(2):
            try:
                conn.request(method, request_uri, body, headers)
                response = conn.getresponse()
            except:
                if i == 0:
                    conn.close()
                    conn.connect()
                    continue
                else:
                    raise
            else:
                content = response.read()
                response = Response(response)
                content = _decompressContent(response, content)

            break;
        return (response, content)


    def _request(self, conn, host, absolute_uri, request_uri, method, body, headers, redirections, cachekey):
        """Do the actual request using the connection object
        and also follow one level of redirects if necessary"""

        auths = [(auth.depth(request_uri), auth) for auth in self.authorizations if auth.inscope(host, request_uri)]
        auth = auths and sorted(auths)[0][1] or None
        if auth: 
            auth.request(method, request_uri, headers, body)

        (response, content) = self._conn_request(conn, request_uri, method, body, headers)

        if auth: 
            if auth.response(response, body):
                auth.request(method, request_uri, headers, body)
                (response, content) = self._conn_request(conn, request_uri, method, body, headers )
                response._stale_digest = 1

        if response.status == 401:
            for authorization in self._auth_from_challenge(host, request_uri, headers, response, content):
                authorization.request(method, request_uri, headers, body) 
                (response, content) = self._conn_request(conn, request_uri, method, body, headers, )
                if response.status != 401:
                    self.authorizations.append(authorization)
                    authorization.response(response, body)
                    break

        if (self.follow_all_redirects or method in ["GET", "HEAD"]) or response.status == 303:
            if response.status in [300, 301, 302, 303, 307]:
                # Pick out the location header and basically start from the beginning
                # remembering first to strip the ETag header and decrement our 'depth'
                if redirections:
                    if not response.has_key('location') and response.status != 300:
                        raise RedirectMissingLocation( _("Redirected but the response is missing a Location: header."))
                    # Fix-up relative redirects (which violate an RFC 2616 MUST)
                    if response.has_key('location'):
                        location = response['location']
                        (scheme, authority, path, query, fragment) = parse_uri(location)
                        if authority == None:
                            response['location'] = urlparse.urljoin(absolute_uri, location)
                    if response.status == 301 and method in ["GET", "HEAD"]:
                        response['-x-permanent-redirect-url'] = response['location']
                        response['-location'] = absolute_uri 
                        _updateCache(headers, response, content, self.cache, cachekey)
                    if headers.has_key('if-none-match'):
                        del headers['if-none-match']
                    if headers.has_key('if-modified-since'):
                        del headers['if-modified-since']
                    if response.has_key('location'):
                        location = response['location']
                        old_response = copy.deepcopy(response)
                        old_response['-location'] = absolute_uri 
                        redirect_method = ((response.status == 303) and (method not in ["GET", "HEAD"])) and "GET" or method
                        (response, content) = self.request(location, redirect_method, body=body, headers = headers, redirections = redirections - 1)
                        response.previous = old_response
                else:
                    raise RedirectLimit( _("Redirected more times than rediection_limit allows."))
            elif response.status in [200, 203] and method == "GET":
                # Don't cache 206's since we aren't going to handle byte range requests
                response['-location'] = absolute_uri 
                _updateCache(headers, response, content, self.cache, cachekey)

        return (response, content)

    def request(self, uri, method="GET", body=None, headers=None, redirections=DEFAULT_MAX_REDIRECTS):
        """ Performs a single HTTP request.
The 'uri' is the URI of the HTTP resource and can begin 
with either 'http' or 'https'. The value of 'uri' must be an absolute URI.

The 'method' is the HTTP method to perform, such as GET, POST, DELETE, etc. 
There is no restriction on the methods allowed.

The 'body' is the entity body to be sent with the request. It is a string
object.

Any extra headers that are to be sent with the request should be provided in the
'headers' dictionary.

The maximum number of redirect to follow before raising an 
exception is 'redirections. The default is 5.

The return value is a tuple of (response, content), the first 
being and instance of the 'Response' class, the second being 
a string that contains the response entity body.
        """
        if headers is None:
            headers = {}
        else:
            headers = _normalize_headers(headers)

        if not headers.has_key('user-agent'):
            headers['user-agent'] = "Python-httplib2/%s" % __version__

        (scheme, authority, request_uri, defrag_uri) = urlnorm(uri)

        if not self.connections.has_key(scheme+":"+authority):
            connection_type = (scheme == 'https') and httplib.HTTPSConnection or httplib.HTTPConnection
            conn = self.connections[scheme+":"+authority] = connection_type(authority)
            conn.set_debuglevel(debuglevel)
        else:
            conn = self.connections[scheme+":"+authority]

        if method in ["GET", "HEAD"] and 'range' not in headers:
            headers['accept-encoding'] = 'compress, gzip'

        info = email.Message.Message()
        cached_value = None
        if self.cache:
            cachekey = defrag_uri
            cached_value = self.cache.get(cachekey)
            if cached_value:
                try:
                    info = email.message_from_string(cached_value)
                    content = cached_value.split('\r\n\r\n', 1)[1]
                except Exception, e:
                    self.cache.delete(cachekey)
                    cachekey = None
                    cached_value = None
        else:
            cachekey = None
                    
        if method in ["PUT"] and self.cache and info.has_key('etag') and not self.ignore_etag:
            # http://www.w3.org/1999/04/Editing/ 
            headers['if-match'] = info['etag']

        if method not in ["GET", "HEAD"] and self.cache and cachekey:
            # RFC 2616 Section 13.10
            self.cache.delete(cachekey)

        if cached_value and method in ["GET", "HEAD"] and self.cache and 'range' not in headers:
            if info.has_key('-x-permanent-redirect-url'):
                # Should cached permanent redirects be counted in our redirection count? For now, yes.
                (response, new_content) = self.request(info['-x-permanent-redirect-url'], "GET", headers = headers, redirections = redirections - 1)
                response.previous = Response(info)
                response.previous.fromcache = True
            else:
                # Determine our course of action:
                #   Is the cached entry fresh or stale?
                #   Has the client requested a non-cached response?
                #   
                # There seems to be three possible answers: 
                # 1. [FRESH] Return the cache entry w/o doing a GET
                # 2. [STALE] Do the GET (but add in cache validators if available)
                # 3. [TRANSPARENT] Do a GET w/o any cache validators (Cache-Control: no-cache) on the request
                entry_disposition = _entry_disposition(info, headers) 
                
                if entry_disposition == "FRESH":
                    if not cached_value:
                        info['status'] = '504'
                        content = ""
                    response = Response(info)
                    if cached_value:
                        response.fromcache = True
                    return (response, content)

                if entry_disposition == "STALE":
                    if info.has_key('etag') and not self.ignore_etag:
                        headers['if-none-match'] = info['etag']
                    if info.has_key('last-modified'):
                        headers['if-modified-since'] = info['last-modified']
                elif entry_disposition == "TRANSPARENT":
                    pass

                (response, new_content) = self._request(conn, authority, uri, request_uri, method, body, headers, redirections, cachekey)

            if response.status == 304 and method == "GET":
                # Rewrite the cache entry with the new end-to-end headers
                # Take all headers that are in response 
                # and overwrite their values in info.
                # unless they are hop-by-hop, or are listed in the connection header.

                for key in _get_end2end_headers(response):
                    info[key] = response[key]
                merged_response = Response(info)
                if hasattr(response, "_stale_digest"):
                    merged_response._stale_digest = response._stale_digest
                try:
                    _updateCache(headers, merged_response, content, self.cache, cachekey)
                except:
                    print locals()
                    raise 
                response = merged_response
                response.status = 200
                response.fromcache = True 

            elif response.status == 200:
                content = new_content
            else:
                self.cache.delete(cachekey)
                content = new_content 
        else: 
            (response, content) = self._request(conn, authority, uri, request_uri, method, body, headers, redirections, cachekey)
        return (response, content)

 

class Response(dict):
    """An object more like email.Message than httplib.HTTPResponse."""
   
    """Is this response from our local cache"""
    fromcache = False

    """HTTP protocol version used by server. 10 for HTTP/1.0, 11 for HTTP/1.1. """
    version = 11

    "Status code returned by server. "
    status = 200

    """Reason phrase returned by server."""
    reason = "Ok"

    previous = None

    def __init__(self, info):
        # info is either an email.Message or 
        # an httplib.HTTPResponse object.
        if isinstance(info, httplib.HTTPResponse):
            for key, value in info.getheaders(): 
                self[key] = value 
            self.status = info.status
            self['status'] = str(self.status)
            self.reason = info.reason
            self.version = info.version
        elif isinstance(info, email.Message.Message):
            for key, value in info.items(): 
                self[key] = value 
            self.status = int(self['status'])

    def __getattr__(self, name):
        if name == 'dict':
            return self 
        else:  
            raise AttributeError, name 


