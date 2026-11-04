#!/bin/bash
cd "$(dirname "$0")/../.." && find storage_organizer -name "*.py" | sort | xargs sha256sum | sha256sum | cut -c1-16
