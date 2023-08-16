#!/bin/bash
rm -rf .tmp

TIMESTAMP=$(date)

build_arch() {
    arch=$1
    echo "building linux_$arch..."

    mkdir .tmp
    cp binary/frpc_linux_$arch data/usr/bin/natfrp-frpc
    cp binary/natfrp_service_linux_$arch data/usr/bin/natfrp-service
    chmod 755 data/usr/bin/natfrp-*

    tar --exclude usr/bin/.gitkeep -C data --format=gnu --sort=name --mtime="$TIMESTAMP" -cpf .tmp/data.tar .
    data_size=$(stat -c "%s" .tmp/data.tar)
    gzip -n .tmp/data.tar

    sed -i -e "s/^Installed-Size: .*/Installed-Size: $data_size/" control/control
    tar -C control --format=gnu --sort=name --mtime="$TIMESTAMP" -cf - . | gzip -n > .tmp/control.tar.gz

    echo "2.0" > .tmp/debian-binary
    tar -C .tmp --format=gnu --sort=name --mtime="$TIMESTAMP" -cf - ./debian-binary ./data.tar.gz ./control.tar.gz | gzip -n > "./release/luci-app-natfrp_${arch}.ipk"

    rm data/usr/bin/natfrp-*
    rm -rf .tmp
}

chmod +x binary/natfrp_service_linux_amd64
version=$(binary/natfrp_service_linux_amd64 -v)
echo "service version: $version"

sed -i -e "s/^Version: .*/Version: $version/" control/control

if [ "$1" != "" ]; then
    build_arch $1
else
    for name in binary/natfrp_service_*; do
        build_arch ${name##*linux_}
    done
fi

git checkout -- control/control
