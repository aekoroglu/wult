#!/bin/sh -euf
#
# -*- coding: utf-8 -*-
# vim: ts=4 sw=4 tw=100 et ai si
#
# Copyright (C) 2020-2021 Intel Corporation
# SPDX-License-Identifier: BSD-3-Clause
#
# Author: Artem Bityutskiy <artem.bityutskiy@linux.intel.com>

PROG="make_a_release.sh"
BASEDIR="$(readlink -ev -- ${0%/*}/..)"

fatal() {
        printf "Error: %s\n" "$1" >&2
        exit 1
}

usage() {
        cat <<EOF
Usage: ${0##*/} <new_ver> <outdir>

<new_ver>  - new tool version to make in X.Y format
EOF
        exit 0
}

ask_question() {
	local question=$1

	while true; do
		printf "%s\n" "$question (yes/no)?"
		IFS= read answer
		if [ "$answer" == "yes" ]; then
			printf "%s\n" "Very good!"
			return
		elif [ "$answer" == "no" ]; then
			printf "%s\n" "Please, do that!"
			exit 1
		else
			printf "%s\n" "Please, answer \"yes\" or \"no\""
		fi
	done
}

[ $# -eq 0 ] && usage
[ $# -eq 1 ] || fatal "insufficient or too many argumetns"

new_ver="$1"; shift

# Validate the new version.
printf "%s" "$new_ver" | egrep -q -x '[[:digit:]]+\.[[:digit:]]+\.[[:digit:]]+' ||
        fatal "please, provide new version in X.Y.Z format"

# Make sure that the current branch is 'master' or 'release'.
current_branch="$(git -C "$BASEDIR" branch | sed -n -e '/^*/ s/^* //p')"
if [ "$current_branch" != "master" -a "$current_branch" != "release" ]; then
	fatal "current branch is '$current_branch' but must be 'master' or 'release'"
fi

# Remind the maintainer about various important things.
ask_question "Did you run tests"
ask_question "Did you update 'CHANGELOG.md'"
ask_question "Did you specify pepc version dependency in 'setup.py' and 'wult.spec'"

# Change the tool version.
sed -i -e "s/^_VERSION = \"[0-9]\+\.[0-9]\+\.[0-9]\+\"$/_VERSION = \"$new_ver\"/" \
    "$BASEDIR/wulttools/_Wult.py"
# Change RPM package version.
sed -i -e "s/^Version:\(\s\+\)[0-9]\+\.[0-9]\+\.[0-9]\+$/Version:\1$new_ver/" \
    "$BASEDIR/rpm/wult.spec"

# Update the man page.
argparse-manpage --pyfile "$BASEDIR/wulttools/_Wult.py" --function _build_arguments_parser \
                 --project-name 'wult' --author 'Artem Bityutskiy' \
                 --author-email 'dedekind1@gmail.com' --output "$BASEDIR/docs/man1/wult.1" \
                 --url 'https://github.com/intel/wult'
argparse-manpage --pyfile "$BASEDIR/wulttools/_Ndl.py" --function _build_arguments_parser \
                 --project-name 'ndl' --author 'Artem Bityutskiy' \
                 --author-email 'dedekind1@gmail.com' --output "$BASEDIR/docs/man1/ndl.1" \
                 --url 'https://github.com/intel/ndl'
pandoc --toc -t man -s "$BASEDIR/docs/man1/wult.1" -t rst -o "$BASEDIR/docs/wult-man.rst"
pandoc --toc -t man -s "$BASEDIR/docs/man1/ndl.1"  -t rst -o "$BASEDIR/docs/ndl-man.rst"

# Update debian changelog.
"$BASEDIR"/../pepc/misc/convert_changelog -o "$BASEDIR/debian/changelog" -p "wult" \
                                          -n "Artem Bityutskiy" -e "artem.bityutskiy@intel.com" \
                                          "$BASEDIR/CHANGELOG.md"

# Commit the changes.
git -C "$BASEDIR" commit -a -s -m "Release version $new_ver"

outdir="."
tag_name="v$new_ver"
release_name="Version $new_ver"

# Create new signed tag.
printf "%s\n" "Signing tag $tag_name"
git -C "$BASEDIR" tag -m "$release_name" -s "$tag_name"

if [ "$current_branch" = "master" ]; then
    branchnames="master and release brances"
else
    branchnames="release branch"
fi

cat <<EOF
To finish the release:
  1. push the $tag_name tag out
  2. push $branchnames branches out

The commands would be:
EOF

for remote in "origin" "upstream" "public"; do
    echo "git push $remote $tag_name"
    if [ "$current_branch" = "master" ]; then
        echo "git push $remote master:master"
        echo "git push $remote master:release"
    else
        echo "git push $remote release:release"
    fi
done

if [ "$current_branch" != "master" ]; then
    echo
    echo "Then merge the release branch back to master, and run the following commands:"

    for remote in "origin" "upstream" "public"; do
        echo "git push $remote master:master"
    done
fi
