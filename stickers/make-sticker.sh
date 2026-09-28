#!/usr/bin/env bash
set -euo pipefail

if [[ $# -ne 1 && $# -ne 2 ]]; then
    echo "Usage: $0 INPUT.png [MATTE_INPUT.png]" >&2
    exit 1
fi

INPUT=$1
MATTE_INPUT="${2:-$1}"
OUTPUT="${INPUT}.webp"

# Tunables. Each can also be overridden through the environment.
FUZZ="${FUZZ:-12}"                   # percent
STROKE="${STROKE:-10}"               # pixels
PAD="${PAD:-32}"                     # working-space padding
SHADOW_BLUR="${SHADOW_BLUR:-12}"
SHADOW_OPACITY="${SHADOW_OPACITY:-0.35}"

# Both images must have identical dimensions.
input_size=$(identify -format '%wx%h' "$INPUT")
matte_size=$(identify -format '%wx%h' "$MATTE_INPUT")

if [[ "$input_size" != "$matte_size" ]]; then
    echo "Error: input sizes differ: $input_size vs $matte_size" >&2
    exit 1
fi

WORK=$(mktemp -d "${TMPDIR:-/tmp}/telegram-sticker.XXXXXX")
trap 'rm -rf "$WORK"' EXIT HUP INT TERM

#
# 1. Pad both inputs.
#
# The matte image is only used for classification. Its colors will never
# appear in the finished sticker.
#
convert "$INPUT" \
    -colorspace sRGB \
    -bordercolor white -border "${PAD}x${PAD}" \
    "$WORK/original.png"

convert "$MATTE_INPUT" \
    -colorspace sRGB \
    -bordercolor white -border "${PAD}x${PAD}" \
    "$WORK/matte-source.png"

#
# 2. Get the confident foreground mask from MATTE_INPUT.
#
# Paint unwanted shadow/material white in MATTE_INPUT before running this.
#
convert "$WORK/matte-source.png" \
    -alpha set \
    -fuzz "${FUZZ}%" \
    -fill none \
    -draw 'matte 0,0 floodfill' \
    -alpha extract \
    "$WORK/core-mask.png"

#
# 3. Apply that mask to the ORIGINAL image.
#
# This is important: edited colors in MATTE_INPUT never become part of
# the sticker.
#
convert "$WORK/original.png" \
    "$WORK/core-mask.png" \
    -alpha off \
    -compose CopyOpacity -composite \
    "$WORK/core.png"

#
# 4. Dilate the foreground mask to make the stroke.
#
convert "$WORK/core-mask.png" \
    -morphology Dilate "Disk:${STROKE}" \
    "$WORK/stroke-mask.png"

#
# 5. Create white stroke layer.
#
convert "$WORK/original.png" \
    -alpha off \
    -fill white -colorize 100 \
    "$WORK/stroke-mask.png" \
    -alpha off \
    -compose CopyOpacity -composite \
    "$WORK/stroke.png"

#
# 6. Synthetic soft black shadow.
#
convert "$WORK/stroke-mask.png" \
    -blur "0x${SHADOW_BLUR}" \
    -evaluate multiply "$SHADOW_OPACITY" \
    "$WORK/shadow-mask.png"

convert "$WORK/original.png" \
    -alpha off \
    -fill black -colorize 100 \
    "$WORK/shadow-mask.png" \
    -alpha off \
    -compose CopyOpacity -composite \
    "$WORK/shadow.png"

#
# 7. Recover semi-transparent material from the ORIGINAL image.
#
# For a pixel C flattened on white:
#
#     C = A*F + (1-A)
#
# Choose the minimum valid alpha:
#
#     A = 1 - min(R,G,B)
#
convert "$WORK/original.png" \
    -alpha off \
    -fx '1-min(r,min(g,b))' \
    "$WORK/recovered-alpha.png"

#
# Recover foreground RGB:
#
#     F = 1 - (1-C)/A
#
convert "$WORK/original.png" \
    "$WORK/recovered-alpha.png" \
    -alpha off \
    -channel RGB \
    -fx 'v.r <= 0.000001 ? 0 : max(0,min(1,1-(1-u)/v.r))' \
    +channel \
    "$WORK/recovered-rgb.png"

convert "$WORK/recovered-rgb.png" \
    "$WORK/recovered-alpha.png" \
    -alpha off \
    -compose CopyOpacity -composite \
    "$WORK/recovered.png"

#
# 8. Composite bottom to top:
#
#       synthetic shadow
#       recovered semi-transparent material
#       white stroke
#       confident original foreground
#
convert "$WORK/shadow.png" \
    "$WORK/recovered.png" \
    -compose Over -composite \
    "$WORK/stroke.png" \
    -compose Over -composite \
    "$WORK/core.png" \
    -compose Over -composite \
    "$WORK/composited.png"

#
# 9. Trim, fit inside a 512x512 canvas, and save lossless WebP.
#
convert "$WORK/composited.png" \
    -trim +repage \
    -resize '512x512>' \
    -gravity center \
    -background none \
    -extent 512x512 \
    -strip \
    -define webp:lossless=true \
    "$OUTPUT"

echo "$OUTPUT"
