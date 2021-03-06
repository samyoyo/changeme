#!/usr/bin/env python

import argparse
import requests
from requests.auth import HTTPBasicAuth
from requests.packages.urllib3.exceptions import InsecureRequestWarning
import yaml
import os
import sys
import re
from netaddr import *
from lxml import html
import threading
import logging
from logutils import colorize
from time import time
from urlparse import urlparse
from cerberus import Validator
from schema import schema
import urllib


__version__ = "0.2.1"


logger = None
banner = """
  #####################################################
 #       _                                             #
 #   ___| |__   __ _ _ __   __ _  ___ _ __ ___   ___   #
 #  / __| '_ \ / _` | '_ \ / _` |/ _ \ '_ ` _ \ / _ \\  #
 # | (__| | | | (_| | | | | (_| |  __/ | | | | |  __/  #
 #  \___|_| |_|\__,_|_| |_|\__, |\___|_| |_| |_|\___|  #
 #                         |___/                       #
 #  v%s                                             #
 #  Default Credential Scanner                         #
  #####################################################
""" % __version__


def setup_logging(verbose, debug, logfile):
    """
    Logging levels:
        - Critical: Default credential found
        - Error: error in the program
        - Warning: Verbose data
        - Info: more verbose
        - Debug: Extra info for debugging purposes
    """
    global logger
    # Set up our logging object
    logger = logging.getLogger(__name__)

    if debug:
        logger.setLevel(logging.DEBUG)
    elif verbose:
        logger.setLevel(logging.INFO)
    else:
        logger.setLevel(logging.WARNING)

    if logfile:
        # Create file handler which logs even debug messages
        #######################################################################
        fh = logging.FileHandler(logfile)

        # create formatter and add it to the handler
        formatter = logging.Formatter('[%(asctime)s][%(levelname)s] %(message)s')
        fh.setFormatter(formatter)
        logger.addHandler(fh)

    # Set up the StreamHandler so we can write to the console
    ###########################################################################
    # create console handler with a higher log level
    ch = colorize.ColorizingStreamHandler(sys.stdout)

    # set custom colorings:
    ch.level_map[logging.DEBUG] = [None, 2, False]
    ch.level_map[logging.INFO] = [None, 'white', False]
    ch.level_map[logging.WARNING] = [None, 'yellow', False]
    ch.level_map[logging.ERROR] = [None, 'red', False]
    ch.level_map[logging.CRITICAL] = [None, 'green', False]
    formatter = logging.Formatter('[%(asctime)s] %(message)s', datefmt='%H:%M:%S')
    ch.setFormatter(formatter)
    logger.addHandler(ch)

    # Adjust the loggers for requests and urllib3
    logging.getLogger('requests').setLevel(logging.ERROR)
    logging.getLogger('urllib3').setLevel(logging.ERROR)
    requests.packages.urllib3.disable_warnings(InsecureRequestWarning)

    return logger


def parse_yaml(f):
    global logger
    with open(f, 'r') as fin:
        raw = fin.read()
        try:
            parsed = yaml.load(raw)
        except(yaml.parser.ParserError):
            logger.error("[parse_yaml] %s is not a valid yaml file" % f)
            return None
    return parsed


def is_yaml(f):
    isyaml = False
    try:
        isyaml = os.path.basename(f).split('.')[1] == 'yml'
    except:
        pass
    return isyaml


def in_scope(name, category, cred):
    add = True

    if name and not cred['name'] == name:
        add = False
    elif category and not cred['category'] == category:
        add = False

    return add


def load_creds(name, category):
    creds = list()
    total_creds = 0
    cred_names = list()
    for root, dirs, files in os.walk('creds'):
        for fname in files:
            f = os.path.join(root, fname)
            if is_yaml(f):
                parsed = parse_yaml(f)
                if parsed:
                    if parsed['name'] in cred_names:
                        logger.error("[load_creds] %s: duplicate name %s" % (f, parsed['name']))
                    elif validate_cred(parsed, f):

                        if in_scope(name, category, parsed):
                            total_creds += len(parsed["auth"]["credentials"])
                            creds.append(parsed)
                            cred_names.append(parsed['name'])
                            logger.debug("[load_creds] loaded creds from %s" % f)

    print('Loaded %i default credential profiles' % len(creds))
    print('Loaded %i default credentials\n' % total_creds)

    return creds


def validate_cred(cred, f):
    v = Validator()
    valid = v.validate(cred, schema)
    for e in v.errors:
        logger.error("[validate_cred] Validation Error: %s, %s - %s" % (f, e, v.errors[e]))

    return valid


def get_fingerprint_matches(res, creds):
    matches = list()
    for cred in creds:
        match = False
        for f in cred['fingerprint']:

            url = "%s" % urlparse(res.request.url)[2]
            if urlparse(res.request.url)[4]:
                url += "?%s" % urlparse(res.request.url)[4]

            if url in cred['fingerprint'].get('url'):
                http_status = cred['fingerprint'].get('status', False)
                logger.debug('[get_fingerprint_matches] fingerprint status: %i, res status: %i' % (http_status, res.status_code))
                if http_status and http_status == res.status_code:
                    match = True

                basic_auth_realm = cred['fingerprint'].get('basic_auth_realm', False)
                if basic_auth_realm and basic_auth_realm in res.headers.get('WWW-Authenticate', list()):
                    match = True

                body_text = cred['fingerprint'].get('body', False)
                if body_text and body_text in res.text:
                    match = True
                    logger.debug('[get_fingerprint_matches] matched body: %s' % body_text)
                elif body_text:
                    logger.debug('[get_fingerprint_matches] body not matched')
                    match = False

        if match:
            matches.append(cred)

    return matches


def check_basic_auth(req, candidate, sessionid=False, csrf=False, proxy=None, timeout=10):
    matches = list()
    for cred in candidate['auth']['credentials']:
        username = cred.get('username', "")
        password = cred.get('password', "")

        if password is None:
            password = ""

        res = requests.get(req, auth=HTTPBasicAuth(username, password), verify=False, proxies=proxy, timeout=timeout)
        if check_success(req, res, candidate, username, password):
            matches.append(cred)

    return matches


def get_parameter_dict(auth):
    params = dict()
    data = auth.get('form', auth.get('get', None))
    for k in data.keys():
        if k not in ('username', 'password', 'url'):
            params[k] = data[k]

    return params


def get_base_url(req):
    parsed = urlparse(req)
    url = "%s://%s" % (parsed[0], parsed[1])
    return url


def check_form(req, candidate, sessionid=False, csrf=False, proxy=None, timeout=10):
    return check_http(req, candidate, sessionid, csrf, proxy, timeout)


def check_get(req, candidate, sessionid=False, csrf=False, proxy=None, timeout=10):
    return check_http(req, candidate, sessionid, csrf, proxy, timeout)


def check_http(req, candidate, sessionid=False, csrf=False, proxy=None, timeout=10):
    matches = list()

    config = candidate['auth'].get('form', candidate['auth'].get('get'))

    url = get_base_url(req)
    logger.debug('[check_http] base url: %s' % url)
    urls = candidate['auth']['url']

    data = get_parameter_dict(candidate['auth'])

    if csrf:
        csrf_field = candidate['auth']['csrf']
        data[csrf_field] = csrf

    for cred in candidate['auth']['credentials']:
        username = cred['username']
        password = cred['password']

        logger.debug('[check_http] %s - %s:%s' % (
                     candidate['name'],
                     username,
                     password,))

        data[config['username']] = username
        data[config['password']] = password

        res = None
        for u in urls:
            url = get_base_url(req) + u
            logger.debug("[check_http] url: %s" % url)
            logger.debug('[check_http] data: %s' % data)

            try:
                if candidate['auth']['type'] == 'form':
                    res = requests.post(url, data, cookies=sessionid, verify=False, proxies=proxy, timeout=timeout)
                else:
                    qs = urllib.urlencode(data)
                    url = "%s?%s" % (url, qs)
                    logger.debug("[check_http] url: %s" % url)
                    res = requests.get(url, cookies=sessionid, verify=False, proxies=proxy, timeout=timeout)
            except Exception as e:
                logger.error("[check_http] Failed to connect to %s" % url)
                logger.debug("[check_http] Exception: %s" % e.__str__().replace('\n', '|'))
                return None

            logger.debug('[check_http] res.status_code: %i' % res.status_code)
            logger.debug('[check_http] res.text: %s' % res.text)

            if res and check_success(req, res, candidate, username, password):
                matches.append(candidate)

    logger.debug('[check_http] matches: %s' % matches)
    return matches


def check_success(req, res, candidate, username, password):
        match = True
        success = candidate['auth']['success']

        if success['status'] and not success['status'] == res.status_code:
            logger.debug('[check_success] status != res.status')
            match = False

        if match and success['body'] and not re.search(success['body'], res.text):
            logger.debug('[check_success] body text not found in response body')
            match = False

        if match:
            logger.critical('[+] Found %s default cred %s:%s at %s' % (candidate['name'], username, password, req))
            return True
        else:
            logger.info('[check_success] Invalid %s default cred %s:%s' % (candidate['name'], username, password))
            return False


def get_csrf_token(res, cred):
    name = cred['auth'].get('csrf', False)
    if name:
        tree = html.fromstring(res.content)
        try:
            csrf = tree.xpath('//input[@name="%s"]/@value' % name)[0]
        except:
            logger.error("[get_csrf_token] failed to get CSRF token %s in %s" % (name, res.url))
            return False
        logger.debug('[get_csrf_token] got CSRF token %s: %s' % (name, csrf))
    else:
        csrf = False

    return csrf


def get_session_id(res, cred):
    cookie = cred['auth'].get('sessionid', False)
    logger.debug("[get_session_id] cookie: %s" % cookie)

    if cookie:
        try:
            value = res.cookies[cookie]
            logger.debug('[get_session_id] cookie value: %s' % value)
        except:
            logger.error("[get_session_id] failed to get %s cookie from %s" % (cookie, res.url))
            return False
        return {cookie: value}
    else:
        logger.debug('[get_session_id] no cookie')
        return False


def scan(urls, creds, config):

    Thread = threading.Thread
    for req in urls:
        while 1:
            if threading.activeCount() <= config['threads']:
                t = Thread(target=do_scan, args=(req, creds, config))
                t.start()
                break


def do_scan(req, creds, config):
        try:
            res = requests.get(req, timeout=config['timeout'], verify=False, proxies=config['proxy'])
            logger.debug('[do_scan] %s - %i' % (req, res.status_code))
        except Exception as e:
            logger.debug('[do_scan] Failed to connect to %s' % req)
            logger.debug(e)
            return

        fp_matches = get_fingerprint_matches(res, creds)
        logger.debug("[do_scan] Found %i fingerprint matches for %s response" % (len(fp_matches), req))
        matches = list()
        for match in fp_matches:
            logger.info('[do_scan] %s matched %s' % (req, match['name']))
            logger.debug('[do_scan] %s auth type: %s' % (match['name'], match['auth']['type']))

            if not config['fingerprint']:
                check = globals()['check_' + match['auth']['type']]
                csrf = get_csrf_token(res, match)
                sessionid = get_session_id(res, match)

                # Only scan if a sessionid is required and we can get it
                if match['auth'].get('sessionid') and not sessionid:
                    logger.debug("[do_scan] Missing required sessionid")
                    continue
                # Only scan if a csrf token is required and we can get it
                if match['auth'].get('csrf', False) and not csrf:
                    logger.debug("[do_scan] Missing required csrf")
                    continue

                new_matches = check(req, match, sessionid, csrf, config['proxy'], config['timeout'])
                if new_matches:
                    matches = matches + new_matches
                    logger.debug('[do_scan] matches: %s' % matches)
            else:
                matches = fp_matches

        return matches


def dry_run(urls):
    logger.info("Dry run URLs:")
    for url in urls:
        print url
    sys.exit()


def build_target_list(targets, creds, name, category):

    # Build target list
    urls = list()
    for target in targets:
        for c in creds:
            if name and not name == c['name']:
                continue
            if category and not category == c['category']:
                continue

            port = c.get('default_port', 80)
            ssl = c.get('ssl', False)
            if ssl:
                proto = 'https'
            else:
                proto = 'http'

            paths = c.get('fingerprint')["url"]

            for path in paths:
                url = '%s://%s:%s%s' % (proto, target, port, path)
                urls.append(url)
                logger.debug('[build_target_list] Rendered url: %s' % url)

    return urls


def print_contributors(creds):
    contributors = set()
    for cred in creds:
        contributors.add(cred['contributor'])

    print "Thank you to our contributors!"
    for i in contributors:
        print i
    print


def print_creds(creds):
    for cred in creds:
        print "\n%s" % cred['name']
        for i in cred['auth']['credentials']:
            print "  - %s:%s" % (i['username'], i['password'])


def main():
    print banner
    targets = list()
    proxy = None
    global logger
    config = dict()

    start = time()

    ap = argparse.ArgumentParser(description='Default credential scanner v%s' % (__version__))
    ap.add_argument('--category', '-c', type=str, help='Category of default creds to scan for', default=None)
    ap.add_argument('--contributors', action='store_true', help='Display cred file contributors')
    ap.add_argument('--debug', '-d', action='store_true', help='Debug output')
    ap.add_argument('--dump', action='store_true', help='Print all of the loaded credentials')
    ap.add_argument('--dryrun', '-r', action='store_true', help='Print urls to be scan, but don\'t scan them')
    ap.add_argument('--fingerprint', '-f', action='store_true', help='Fingerprint targets, but don\'t check creds')
    ap.add_argument('--log', '-l', type=str, help='Write logs to logfile', default=None)
    ap.add_argument('--name', '-n', type=str, help='Narrow testing to the supplied credential name', default=None)
    ap.add_argument('--proxy', '-p', type=str, help='HTTP(S) Proxy', default=None)
    ap.add_argument('--subnet', '-s', type=str, help='Subnet or IP to scan')
    ap.add_argument('--targets', type=str, help='File of targets to scan')
    ap.add_argument('--threads', '-t', type=int, help='Number of threads', default=10)
    ap.add_argument('--timeout', type=int, help='Timeout in seconds for a request', default=10)
    ap.add_argument('--verbose', '-v', action='store_true', help='Verbose output')
    ap.add_argument('--validate', action='store_true', help='Validate creds files')
    args = ap.parse_args()

    setup_logging(args.verbose, args.debug, args.log)

    if not args.subnet and not args.targets and not args.validate and not args.contributors and not args.dump:
        logger.error('Need to supply a subnet or targets file.')
        ap.print_help()
        sys.exit()

    if args.subnet:
        for ip in IPNetwork(args.subnet).iter_hosts():
            targets.append(ip)

    if args.targets:
        with open(args.targets, 'r') as fin:
            targets = [x.strip('\n') for x in fin.readlines()]

    logger.info("Loaded %i targets" % len(targets))

    if args.proxy and re.match('^https?://[0-9\.]+:[0-9]{1,5}$', args.proxy):
        proxy = {'http': args.proxy,
                 'https': args.proxy}
        logger.info('Setting proxy to %s' % args.proxy)
    elif args.proxy:
        logger.error('Invalid proxy, must be http(s)://x.x.x.x:8080')
        sys.exit()

    if args.validate:
        load_creds()
        sys.exit()

    creds = load_creds(args.name, args.category)

    if args.contributors:
        print_contributors(creds)

    if args.dump:
        print_creds(creds)

    if args.fingerprint:
        # Need to drop the level to INFO to see the fp messages
        logger.setLevel(logging.INFO)

    urls = build_target_list(targets, creds, args.name, args.category)

    if args.dryrun:
        dry_run(urls)

    logger.info('Scanning %i URLs' % len(urls))

    config = {
        'threads':  args.threads,
        'timeout': args.timeout,
        'proxy': proxy,
        'fingerprint': args.fingerprint}

    scan(urls, creds, config)


if __name__ == '__main__':
    main()
