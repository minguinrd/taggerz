#!/bin/sh
# Regenerates tests/fixtures/silence.flac (0.2s of silence).
ffmpeg -f lavfi -i anullsrc=r=44100:cl=stereo -t 0.2 -c:a flac "$(dirname "$0")/silence.flac" -y
