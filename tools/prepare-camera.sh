#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-2.0-or-later

set -Eeuo pipefail

product_name=$(< /sys/class/dmi/id/product_name)
case $product_name in
	*YB1-X91L*|*YB1_X91L*) ;;
	*)
		echo "ERROR: unsupported DMI product: $product_name" >&2
		exit 1
		;;
esac

if [[ ! -e /dev/video10 ]]; then
	modprobe v4l2loopback \
		video_nr=10 \
		card_label='Yoga Book Camera' \
		exclusive_caps=1 \
		max_buffers=2
	udevadm settle --timeout=5
fi

if [[ ! -e /dev/video10 ]] && command -v v4l2loopback-ctl >/dev/null; then
	v4l2loopback-ctl add \
		--name 'Yoga Book Camera' \
		--exclusive-caps 1 \
		--buffers 2 \
		/dev/video10
	udevadm settle --timeout=5
fi

if [[ ! -e /sys/module/atomisp/parameters/allow_raw_output ]]; then
	modprobe atomisp allow_raw_output=1
fi

[[ -e /sys/module/atomisp/parameters/allow_raw_output ]] || {
	echo 'ERROR: AtomISP raw-output gate is missing; install the Yoga Book kernel' >&2
	exit 1
}
printf '1\n' > /sys/module/atomisp/parameters/allow_raw_output

for _attempt in {1..30}; do
	[[ -e /dev/video0 && -e /dev/media0 ]] && break
	sleep 1
done

[[ -e /dev/video0 && -e /dev/media0 ]] || {
	echo 'ERROR: AtomISP capture node /dev/video0 is missing' >&2
	exit 1
}
[[ -e /dev/video10 ]] || {
	echo 'ERROR: Yoga Book loopback node /dev/video10 is missing' >&2
	exit 1
}

loopback_name=$(< /sys/class/video4linux/video10/name)
case $loopback_name in
	Yoga\ Book*Camera) ;;
	*)
		echo "ERROR: /dev/video10 belongs to a different device: $loopback_name" >&2
		exit 1
		;;
esac

echo 'Yoga Book camera devices prepared'
