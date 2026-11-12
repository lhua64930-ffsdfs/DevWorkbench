#!/bin/sh
set -eu

work=$(mktemp -d)
cd "$work"

# The hashes are from Debian's signed expat_2.10.0-1.dsc.
pool=https://deb.debian.org/debian/pool/main/e/expat
curl -fsSLO "$pool/expat_2.10.0.orig.tar.gz"
curl -fsSLO "$pool/expat_2.10.0-1.debian.tar.xz"
sha256sum -c <<'EOF'
b3644a12f62d7762b62c444f69d169ba0234f13138d0240244bca64cbe35286f  expat_2.10.0.orig.tar.gz
9be65c3ff79ca0508590970422de0a55f117bb7134a0274dd1ed3261f85631dc  expat_2.10.0-1.debian.tar.xz
EOF

mkdir expat-2.10.0
tar -xzf expat_2.10.0.orig.tar.gz --strip-components=1 -C expat-2.10.0
cd expat-2.10.0
tar -xJf ../expat_2.10.0-1.debian.tar.xz
{
    printf '%s\n' 'expat (2.10.0-1~deb13u0+codepilot1) trixie; urgency=high' '' \
        '  * Rebuild the Debian source package on trixie.' '' \
        ' -- CodePilot Build <build@codepilot.invalid>  Wed, 11 Nov 2026 00:00:00 +0000' ''
    cat debian/changelog
} > debian/changelog.new
mv debian/changelog.new debian/changelog
dpkg-buildpackage -B -us -uc -j4

mkdir -p /out
cp ../libexpat1_*_*.deb /out/
dpkg -i ../libexpat1_*_*.deb ../libexpat1-dev_*_*.deb

# CPython in the official image bundles a second copy of Expat in pyexpat.
curl -fsSLo python.tar.xz "https://www.python.org/ftp/python/$PYTHON_VERSION/Python-$PYTHON_VERSION.tar.xz"
printf '%s  %s\n' "$PYTHON_SHA256" python.tar.xz | sha256sum -c -
tar -xJf python.tar.xz
cd "Python-$PYTHON_VERSION"
extension=$(python -c 'import sysconfig; print(sysconfig.get_config_var("EXT_SUFFIX"))')
cc -shared -fPIC -O2 -fstack-protector-strong -D_FORTIFY_SOURCE=2 \
    $(python3-config --includes) -IInclude/internal -IModules \
    -Wl,-z,relro,-z,now Modules/pyexpat.c -lexpat -o "/out/pyexpat$extension"
PYTHONPATH=/out python -c 'import pyexpat; assert pyexpat.EXPAT_VERSION == "expat_2.10.0", pyexpat.EXPAT_VERSION'
PYTHONPATH="/out:$PWD/Lib" python -P -m test test_pyexpat test_xml_etree test_xml_etree_c
