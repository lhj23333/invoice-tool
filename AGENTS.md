# Invoice Workbench

- Build a native Windows desktop GUI. The current stage is local file organization; Outlook and OCR are deferred.
- Run on demand. Do not add a continuously running collection service or require a server for core tasks.
- Whole invoices belong to one active batch. Excess above the declared amount is never carried forward.
- The user clarified on 2026-11-17 that backups were a development-only precaution, not a product feature. Do not add automatic backup, backup dialogs, or backup folders to the normal import flow.
- The explicitly supplied `C:\Users\lhj32\workspace\BaiduSyncdisk\DeepX\invoice_bak` is authorized for business validation. Read it directly and verify source file hashes before/after. Never rename, move, delete, or overwrite its files.
- Use synthetic fixtures under `samples` and `.test-tmp` during development. Never discover or scan real invoice locations without the user providing them.
- Keep all index/objects/history data together for sequential cloud-drive synchronization. Detect changes and stop rather than overwriting externally modified state.
- Run `python -m unittest discover -v` after substantive changes. Use the bundled portable runtime if the host runtime's Tcl installation cannot be read by Tcl.
- Keep dependencies pinned. Preserve dependency license files when building a portable distribution.
- Keep the left sidebar as the only main navigation (发票资料 / 报销批次 / 设置), with three independent page frames and no top-level tab strip. Scope stats/actions to their own page; use the native window close button. Source folders are for import; a workspace holds the index and stored originals.
