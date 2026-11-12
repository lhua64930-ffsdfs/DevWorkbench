#!/bin/sh
set -eu

work=$(mktemp -d)
cd "$work"

# MIT release source, checked against Alpine's 3.24-stable krb5 APKBUILD.
curl --retry 2 -fsSLO https://web.mit.edu/kerberos/dist/krb5/1.22/krb5-1.22.3.tar.gz
printf '%s\n' '0276d83dee90e76d50a506d22901e658212094c1191fc09b8510273b4bfc56195ab1dd1c80b5fd8288b4aa9323c62f0b60ab3f6227ea113b4712bc54cb144abb  krb5-1.22.3.tar.gz' | sha512sum -c -
# Debian packaging checked against the signed krb5_1.22.1-3.dsc.
curl --retry 2 -fsSLO https://deb.debian.org/debian/pool/main/k/krb5/krb5_1.22.1-3.debian.tar.xz
printf '%s\n' '519ad7537260ebc450a678b38f00c8deaf0e05d848d0e5cfeef1ddb154663ba8  krb5_1.22.1-3.debian.tar.xz' | sha256sum -c -
tar -xzf krb5-1.22.3.tar.gz
cd krb5-1.22.3
tar -xJf ../krb5_1.22.1-3.debian.tar.xz

# The MIT release already includes the OpenSSL compatibility change from
# commit 5e4e8452328804948d042235bbf58ca457795857.
while IFS= read -r name; do
    case "$name" in
        0014-Improve-future-OpenSSL-compatibility.patch|0015-Use-X509_check_host-to-verify-KKDCP-server-cert.patch) continue ;;
    esac
    printf 'Checking Debian patch %s\n' "$name"
    if patch --batch --forward --fuzz=0 -p1 --dry-run < "debian/patches/$name" >/dev/null; then
        patch --batch --forward --fuzz=0 -p1 < "debian/patches/$name"
    else
        patch --batch --reverse --fuzz=0 -p1 --dry-run < "debian/patches/$name"
    fi
done < debian/patches/series
truncate -s 0 debian/patches/series

# Docker's seccomp profile reports EPERM for keyctl instead of ENOSYS.
# The Debian test helper must recognize both and report other failures as text.
sed -i \
    -e "s/'Function not implemented' in out and e.returncode == 1/e.returncode == 1 and ('Function not implemented' in out or 'Operation not permitted' in out)/" \
    -e "s/'Unexpected output from keyctl: '+e.output/'Unexpected output from keyctl: ' + out/" \
    src/util/k5test.py

# Rebase the Debian 0015 patch (MIT f5bbfa4821cf590a4748f96d0e016bc0485e95c4)
# over the newer OpenSSL compatibility code in 1.22.3.
python - <<'PY'
from pathlib import Path

path = Path("src/plugins/tls/k5tls/openssl.c")
source = path.read_text()
start = source.index("/* Return the passed-in character, lower-cased")
end = source.index("static int\nverify_callback(", start)
assert "check_cert_name_or_ip(" in source[start:end]
path.write_text(source[:start] + """static krb5_boolean
check_cert_name_or_ip(const X509 *x, const char *expected_name)
{
    struct in_addr in;
    struct in6_addr in6;
    int r;

    if (inet_pton(AF_INET, expected_name, &in) != 0 ||
        inet_pton(AF_INET6, expected_name, &in6) != 0)
        r = X509_check_ip_asc(x, expected_name, 0);
    else
        r = X509_check_host(x, expected_name, 0, 0, NULL);
    return r == 1;
}

""" + source[end:])
PY

{
    printf '%s\n' 'krb5 (1.22.3-0~deb13u0+codepilot1) trixie; urgency=high' '' \
        '  * Build the MIT Kerberos security release on trixie.' '' \
        ' -- CodePilot Build <build@codepilot.invalid>  Wed, 11 Nov 2026 00:00:00 +0000' ''
    cat debian/changelog
} > debian/changelog.new
mv debian/changelog.new debian/changelog
printf '%s\n' ' k5_ser_unpack_len@krb5_3_MIT 1.22.3-0~deb13u0+codepilot1' >> debian/libkrb5-3.symbols
PYTHON=/usr/bin/python3 dpkg-buildpackage -B -us -uc -j4

mkdir -p /out
cp ../libgssapi-krb5-2_*_*.deb ../libk5crypto3_*_*.deb \
    ../libkrb5-3_*_*.deb ../libkrb5support0_*_*.deb /out/
