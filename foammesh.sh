#!/usr/bin/env bash

SOURCE=${BASH_SOURCE[0]}

while [ -L "$SOURCE" ]; do # resolve $SOURCE until the file is no longer a symlink
    DIR=$( cd -P "$( dirname "$SOURCE" )" >/dev/null 2>&1 && pwd )
    SOURCE=$(readlink "$SOURCE")
    [[ $SOURCE != /* ]] && SOURCE=$DIR/$SOURCE # if $SOURCE was a relative symlink, we need to resolve it relative to the path where the symlink file was located
done

cd -P "$( dirname "$SOURCE" )" >/dev/null 2>&1

[ -f venv/bin/activate ] && source venv/bin/activate

# src/ (first-party) + vendor/ (PyFoam) source roots on the import path.
export PYTHONPATH="src:vendor${PYTHONPATH:+:$PYTHONPATH}"

python -m foammesh.main
