# Based on Alpine 3.24's libxml2 2.13.9-r2 packaging.
pkgname=libxml2
pkgver=2.13.9_p20261111
pkgrel=0
pkgdesc="XML parsing library, version 2 (CodePilot security backport)"
url="https://gitlab.gnome.org/GNOME/libxml2"
arch="all"
license="MIT"
makedepends="xz-dev zlib-dev"
checkdepends="perl tar"
subpackages="$pkgname-doc $pkgname-dev $pkgname-static $pkgname-utils"
source="https://download.gnome.org/sources/libxml2/2.13/libxml2-2.13.9.tar.xz
    https://raw.githubusercontent.com/alpinelinux/aports/3.24-stable/main/libxml2/CVE-2026-6732.patch
    https://raw.githubusercontent.com/alpinelinux/aports/3.24-stable/main/libxml2/CVE-2026-6732-test.patch
    CVE-2026-86140.patch::https://gitlab.gnome.org/GNOME/libxml2/-/commit/d1686f91dbda141a752200419d35639fd6b38340.patch"
builddir="$srcdir/libxml2-2.13.9"

prepare() {
    default_prepare
    rm test/icu_parse_test.xml
}

build() {
    ./configure --build="$CBUILD" --host="$CHOST" --prefix=/usr \
        --sysconfdir=/etc --enable-static --with-legacy --with-lzma \
        --with-zlib --with-python=no
    make
}

check() {
    rm -f test/ebcdic_566012.xml
    make runtests
}

package() {
    make -j1 DESTDIR="$pkgdir" install
}

utils() {
    amove usr/bin
}

sha512sums="
62d4813860124c969f204aaf33b497105dbc32a6c5655f5a86168743660e10987d687d7e5e7ee49fdfdeb8f6ad9fa4503f81fcce2e4d459094895f02436d1b13  libxml2-2.13.9.tar.xz
a54eca0b69fb49c9894684069b0e68dd80f002977c59249bee6d1a168391179e9815dfbe49a9cce3de28ca4a1bdfcac8a691daa240dd70a0206d24d32f6e84cb  CVE-2026-6732.patch
9117a6a060d9c760ae4aa88c33131b02b4677a6703fe02ea36a474035c901d5da778a4d98402a87187410e9bcfe9819d382f9df4e6fe42ff3cf33acc88ba28a9  CVE-2026-6732-test.patch
f9bea24803b8d48b0a9f8f11bfcd9a5755f2372c82c0cf7075344088759d9743a3e0dc1a71e9cd4c05cdc40e98174d14430eea121b6df1dd629ed0499715f995  CVE-2026-86140.patch
"
