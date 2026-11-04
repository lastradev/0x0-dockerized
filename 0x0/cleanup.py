#!/usr/bin/env python3

"""
    Copyright © 2020 Mia Herkt
    Licensed under the EUPL, Version 1.2 or - as soon as approved
    by the European Commission - subsequent versions of the EUPL
    (the "License");
    You may not use this work except in compliance with the License.
    You may obtain a copy of the license at:

        https://joinup.ec.europa.eu/software/page/eupl

    Unless required by applicable law or agreed to in writing,
    software distributed under the License is distributed on an
    "AS IS" basis, WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND,
    either express or implied.
    See the License for the specific language governing permissions
    and limitations under the License.
"""

import os
import sys
import time
import datetime
from pathlib import Path
from fhost import app

def cleanup_expired(storage_path, maxs, mind=30, maxd=365):
    names = os.listdir(storage_path)
    systime = time.time()
    remaining = set()

    for name in names:
        path = os.path.join(storage_path, name)
        if not os.path.isfile(path):
            continue
        stat = os.stat(path)
        age = datetime.timedelta(seconds=(systime - stat.st_mtime)).days
        maxage = mind + (-maxd + mind) * (stat.st_size / maxs - 1) ** 3
        if age >= maxage:
            os.remove(path)
        else:
            remaining.add(name)

    index_dir = os.path.join(storage_path, ".csv-index")
    if not os.path.isdir(index_dir):
        return
    for name in os.listdir(index_dir):
        sha = name.split(".")[0]
        if sha not in remaining:
            os.remove(os.path.join(index_dir, name))



# ponytail: legacy token links. Remove when
#   SELECT count(*) FROM file WHERE token IS NOT NULL;  -> 0
# then: migration dropping file.token and recreating uq_file_link without WHERE.

def prune_rows(storage_path):
    import fhost
    storage = Path(storage_path)
    with fhost.app.app_context():
        distinct_shas = [r[0] for r in fhost.db.session.query(fhost.File.sha256).distinct().all() if r[0]]

        for sha in distinct_shas:
            target_file = storage / sha
            # TOCTOU-reduced stat check: skip if file exists on disk
            if target_file.is_file():
                continue

            # 1. Unmoderated expired files: record non-PII audit and delete row (data minimization)
            expired_rows = fhost.File.query.filter_by(sha256=sha, removed=False).all()
            for row in expired_rows:
                fhost.db.session.add(fhost.FileAudit(sha256=row.sha256, link=row.link, action="PRUNED_EXPIRED"))
            if expired_rows:
                # Re-verify absence immediately before delete to reduce TOCTOU race window
                if not target_file.is_file():
                    fhost.File.query.filter_by(sha256=sha, removed=False).delete(synchronize_session=False)

            # 2. Moderated files: scrub IP to protect privacy, keep minimal blocklist row
            moderated_rows = fhost.File.query.filter(fhost.File.sha256 == sha, fhost.File.removed == True, fhost.File.addr != None).all()
            for row in moderated_rows:
                fhost.db.session.add(fhost.FileAudit(sha256=row.sha256, link=row.link, action="MODERATED_ADDR_SCRUBBED"))
                row.addr = None

        fhost.db.session.commit()


if __name__ == "__main__":
    os.chdir(os.path.dirname(os.path.abspath(sys.argv[0])))
    cleanup_expired(app.config["FHOST_STORAGE_PATH"], app.config["MAX_CONTENT_LENGTH"])
    prune_rows(app.config["FHOST_STORAGE_PATH"])
