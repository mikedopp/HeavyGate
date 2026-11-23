"""HeavyGate's signing step for the pool: APKs and app bundles built on a worker are signed again here, with this
PC's key.

A worker never gets a signing key (heavypool.py doesn't send them), so what it builds is signed with its own debug key or
not at all, and Android won't install that over an app signed with yours. When the APKs come back, each one is signed
again here, with the key a local build would have used:

    a release APK, with keystore.properties beside the Gradle build   that keystore (storeFile, storePassword, keyAlias,
                                                                     keyPassword; storeFile relative to the build or app/)
    anything else                                                    this PC's debug key (~/.android/debug.keystore,
                                                                     alias androiddebugkey, password android), which a
                                                                     local build signs with

Then the APK's signer certificate is checked against the keystore's own certificate (SHA-256), and an APK that can't be
signed is renamed to <name>.apk.worker-signed so nobody installs it by mistake. Passwords go to apksigner and keytool
through environment variables, never the command line. apksigner comes from the Android SDK's newest build-tools, run with
the JDK of JAVA_HOME or of javac on PATH (pool_java overrides it).

An app bundle (.aab) is signed the way Gradle signs one, with the JDK's jarsigner and the same key choice: any signature
the worker put on it is taken off first, and the signer certificate is checked against the key's afterwards
(keytool -printcert -jarfile). One that can't be signed is renamed <name>.aab.worker-signed. Standard library only."""
import base64
import glob
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import zipfile

NO_WINDOW = 0x08000000 if os.name == 'nt' else 0
DEBUG_KEY = {'store': os.path.join(os.path.expanduser('~'), '.android', 'debug.keystore'), 'alias': 'androiddebugkey',
             'store_pass': 'android', 'key_pass': 'android', 'what': 'the debug key (~/.android/debug.keystore)'}


class SignError(Exception):
    pass


def sdk_dirs():
    for c in (os.environ.get('ANDROID_HOME'), os.environ.get('ANDROID_SDK_ROOT'),
              os.path.join(os.environ.get('LOCALAPPDATA') or '', 'Android', 'Sdk'),
              os.path.expanduser('~/Android/Sdk'), os.path.expanduser('~/android-sdk')):
        if c and os.path.isdir(os.path.join(c, 'build-tools')):
            yield c


def _ver(name):
    return tuple(int(x) for x in re.findall(r'\d+', name)) or (0,)


def build_tools():
    """The newest build-tools folder that has apksigner.jar, or None."""
    for sdk in sdk_dirs():
        found = [d for d in glob.glob(os.path.join(sdk, 'build-tools', '*'))
                 if os.path.isfile(os.path.join(d, 'lib', 'apksigner.jar'))]
        if found:
            return max(found, key=lambda d: _ver(os.path.basename(d)))
    return None


def java_bin(s=None):
    """(java, keytool) from pool_java, JAVA_HOME, or the JDK that javac on PATH belongs to (a lone JRE 8 java on PATH
    is too old for apksigner)."""
    exe = '.exe' if os.name == 'nt' else ''
    homes = [(s or {}).get('pool_java'), os.environ.get('JAVA_HOME')]
    javac = shutil.which('javac')
    if javac:
        homes.append(os.path.dirname(os.path.dirname(os.path.realpath(javac))))
    for h in homes:
        if h and os.path.isfile(os.path.join(h, 'bin', 'java' + exe)) and \
                os.path.isfile(os.path.join(h, 'bin', 'keytool' + exe)):
            return os.path.join(h, 'bin', 'java' + exe), os.path.join(h, 'bin', 'keytool' + exe)
    return None, None


def read_properties(path):
    """Java .properties: key=value or key: value, # and ! comments, backslash escapes."""
    out = {}
    with open(path, encoding='utf-8', errors='replace') as f:
        for line in f:
            line = line.strip()
            if not line or line[0] in '#!':
                continue
            m = re.match(r'((?:\\.|[^=:\s])+)\s*[=:\s]\s*(.*)$', line)
            if m:
                unq = lambda t: re.sub(r'\\(.)', lambda x: {'n': '\n', 't': '\t'}.get(x.group(1), x.group(1)), t)
                out[unq(m.group(1))] = unq(m.group(2))
    return out


SIG_FILE = re.compile(r'^META-INF/(MANIFEST\.MF|[^/]+\.(SF|RSA|DSA|EC))$', re.I)


def key_for(apk_rel, gradle_root):
    """The key a local build would sign this APK with (see the module notes)."""
    parts = [p.lower() for p in apk_rel.replace('\\', '/').split('/')]
    release = any('release' in p for p in parts)
    props = os.path.join(gradle_root, 'keystore.properties')
    if release and os.path.isfile(props):
        p = read_properties(props)
        store = p.get('storeFile') or ''
        cands = [store] if os.path.isabs(store) else [os.path.join(gradle_root, store),
                                                       os.path.join(gradle_root, 'app', store)]
        found = next((c for c in cands if store and os.path.isfile(c)), None)
        if not found or not p.get('keyAlias'):
            raise SignError(f'keystore.properties in {gradle_root} names a key that is not there ({store or "no storeFile"})')
        return {'store': found, 'alias': p['keyAlias'], 'store_pass': p.get('storePassword', ''),
                'key_pass': p.get('keyPassword') or p.get('storePassword', ''),
                'what': f'the release key in keystore.properties ({os.path.basename(found)})'}
    if not os.path.isfile(DEBUG_KEY['store']):
        raise SignError(f"there is no debug key at {DEBUG_KEY['store']} (a local Android build makes one)")
    return dict(DEBUG_KEY)


def _run(cmd, env, timeout=180):
    r = subprocess.run(cmd, env=env, capture_output=True, timeout=timeout, creationflags=NO_WINDOW)
    return r.returncode, r.stdout.decode('utf-8', 'replace'), r.stderr.decode('utf-8', 'replace')


def key_cert_sha256(key, keytool, env):
    code, out, err = _run([keytool, '-exportcert', '-rfc', '-keystore', key['store'], '-alias', key['alias'],
                           '-storepass:env', 'HG_KS_PASS'], env)
    if code:
        lines = (err or out).strip().splitlines()
        raise SignError('keytool could not read the key: ' + (lines[-1] if lines else f'exit {code}'))
    b64 = ''.join(x for x in out.splitlines() if x and not x.startswith('-----'))
    return hashlib.sha256(base64.b64decode(b64)).hexdigest()


def apk_cert_sha256(apk, java, tools, env):
    code, out, err = _run([java, '-jar', os.path.join(tools, 'lib', 'apksigner.jar'), 'verify', '--print-certs', apk], env)
    if code:
        raise SignError('apksigner verify failed: ' + (err or out).strip()[-300:])
    m = re.search(r'Signer #1 certificate SHA-256 digest:\s*([0-9a-f]{64})', out)
    if not m:
        raise SignError('apksigner verify printed no signer certificate')
    return m.group(1)


def sign_apk(path, rel, gradle_root, s=None):
    """Signs one APK (path; rel is its path inside the project, which tells debug from release) in place, or as
    app-release.apk next to an app-release-unsigned.apk. {'apk', 'key', 'cert'}; raises SignError."""
    tools = build_tools()
    if not tools:
        raise SignError('no Android build-tools with apksigner (ANDROID_HOME or %LOCALAPPDATA%\\Android\\Sdk)')
    java, keytool = java_bin(s)
    if not java:
        raise SignError('no JDK for apksigner (JAVA_HOME, javac on PATH, or the pool_java setting)')
    key = key_for(rel, gradle_root)
    env = dict(os.environ, HG_KS_PASS=key['store_pass'], HG_KEY_PASS=key['key_pass'])
    out = path[:-len('-unsigned.apk')] + '.apk' if path.endswith('-unsigned.apk') else path
    zipalign = os.path.join(tools, 'zipalign' + ('.exe' if os.name == 'nt' else ''))
    with tempfile.TemporaryDirectory(prefix='heavysign-') as tmp:
        src = path
        if os.path.isfile(zipalign) and _run([zipalign, '-c', '-p', '4', path], env)[0] != 0:
            src = os.path.join(tmp, 'aligned.apk')
            code, o, e = _run([zipalign, '-p', '-f', '4', path, src], env)
            if code:
                raise SignError('zipalign failed: ' + (e or o).strip()[-300:])
        signed = os.path.join(tmp, 'signed.apk')
        code, o, e = _run([java, '-jar', os.path.join(tools, 'lib', 'apksigner.jar'), 'sign', '--ks', key['store'],
                           '--ks-key-alias', key['alias'], '--ks-pass', 'env:HG_KS_PASS', '--key-pass', 'env:HG_KEY_PASS',
                           '--out', signed, src], env)
        if code:
            raise SignError('apksigner sign failed: ' + (e or o).strip()[-300:])
        got, want = apk_cert_sha256(signed, java, tools, env), key_cert_sha256(key, keytool, env)
        if got != want:
            raise SignError(f'signed, but its certificate {got[:16]} is not the key\'s {want[:16]}')
        shutil.move(signed, out)
    if out != path:
        os.remove(path)
        meta = os.path.join(os.path.dirname(out), 'output-metadata.json')
        try:
            with open(meta, encoding='utf-8') as f:
                text = f.read()
            with open(meta, 'w', encoding='utf-8') as f:
                f.write(text.replace(os.path.basename(path), os.path.basename(out)))
        except OSError:
            pass
    return {'apk': out, 'key': key['what'], 'cert': got}


def jar_cert_sha256(path, keytool, env):
    """The SHA-256 of a signed jar's (or bundle's) signer certificate, as keytool prints it."""
    code, out, err = _run([keytool, '-printcert', '-jarfile', path], env)
    m = re.search(r'SHA256:\s*([0-9A-Fa-f:]{95})', out)
    if code or not m:
        raise SignError('keytool could not read the bundle\'s signer: ' + ((err or out).strip()[-200:] or f'exit {code}'))
    return m.group(1).replace(':', '').lower()


def sign_aab(path, rel, gradle_root, s=None):
    """Signs one app bundle in place with jarsigner, after taking off any signature the worker put on it.
    {'apk', 'key', 'cert'}; raises SignError."""
    java, keytool = java_bin(s)
    jarsigner = keytool and os.path.join(os.path.dirname(keytool), 'jarsigner' + ('.exe' if os.name == 'nt' else ''))
    if not jarsigner or not os.path.isfile(jarsigner):
        raise SignError('no JDK with jarsigner (JAVA_HOME, javac on PATH, or the pool_java setting)')
    key = key_for(rel, gradle_root)
    env = dict(os.environ, HG_KS_PASS=key['store_pass'], HG_KEY_PASS=key['key_pass'])
    with tempfile.TemporaryDirectory(prefix='heavysign-') as tmp:
        bare = os.path.join(tmp, 'unsigned.aab')
        try:
            with zipfile.ZipFile(path) as zin, zipfile.ZipFile(bare, 'w') as zout:
                for info in zin.infolist():
                    if not SIG_FILE.match(info.filename):
                        zout.writestr(info, zin.read(info), compress_type=info.compress_type)
        except zipfile.BadZipFile as e:
            raise SignError(f'not a readable bundle: {e}')
        signed = os.path.join(tmp, 'signed.aab')
        code, o, e = _run([jarsigner, '-keystore', key['store'], '-storepass:env', 'HG_KS_PASS', '-keypass:env',
                           'HG_KEY_PASS', '-digestalg', 'SHA-256', '-signedjar', signed, bare, key['alias']], env)
        if code:
            raise SignError('jarsigner failed: ' + (e or o).strip()[-300:])
        code, o, e = _run([jarsigner, '-verify', signed], env)
        if code or 'jar verified' not in o:
            raise SignError('jarsigner -verify failed: ' + (e or o).strip()[-300:])
        got, want = jar_cert_sha256(signed, keytool, env), key_cert_sha256(key, keytool, env)
        if got != want:
            raise SignError(f'signed, but its certificate {got[:16]} is not the key\'s {want[:16]}')
        shutil.move(signed, path)
    return {'apk': path, 'key': key['what'], 'cert': got}


def sign_all(root, files, gradle_root, s=None):
    """Signs every .apk and .aab among files (paths relative to root, as heavypool brought them back). (done,
    failed): done [{'apk', 'key', 'cert'}], failed [(rel, why)], each failed one renamed to .apk/.aab.worker-signed."""
    done, failed = [], []
    for rel in files:
        low = rel.lower()
        if not low.endswith(('.apk', '.aab')):
            continue
        path = os.path.join(root, *rel.split('/'))
        try:
            done.append(dict((sign_aab if low.endswith('.aab') else sign_apk)(path, rel, gradle_root, s), rel=rel))
        except (SignError, OSError, subprocess.TimeoutExpired) as e:
            failed.append((rel, str(e)))
            try:
                os.replace(path, path + '.worker-signed')
            except OSError:
                pass
    return done, failed


def describe(done):
    keys = sorted({d['key'] for d in done})
    certs = sorted({d['cert'][:16] for d in done})
    n_aab = sum(1 for d in done if d['apk'].lower().endswith('.aab'))
    n_apk = len(done) - n_aab
    what = ' and '.join(x for x in (f"{n_apk} APK{'s' if n_apk != 1 else ''}" if n_apk else '',
                                     f"{n_aab} bundle{'s' if n_aab != 1 else ''}" if n_aab else '') if x)
    return f"signed {what} here with {', '.join(keys)} (certificate SHA-256 " \
           f"{', '.join(certs)}..., checked)"


if __name__ == '__main__':   # python heavysign.py APK [GRADLE_ROOT]: sign one by hand
    import sys
    a = sys.argv[1:]
    if not a:
        print(__doc__)
        sys.exit(2)
    try:
        r = (sign_aab if a[0].lower().endswith('.aab') else sign_apk)(
            os.path.abspath(a[0]), a[0], os.path.abspath(a[1] if len(a) > 1 else '.'))
        print(json.dumps(r, indent=2))
    except SignError as e:
        print(f'heavysign: {e}', file=sys.stderr)
        sys.exit(1)
