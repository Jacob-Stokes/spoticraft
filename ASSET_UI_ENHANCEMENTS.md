# Asset Manager Enhancements

Two UX improvements are in scope for the assets browser:

1. Allow users to create folders within the configured assets directory.
2. Show an image preview tooltip when hovering over supported asset files.

The following sections outline the work needed across API and UI layers, plus validation, testing, and rollout considerations.

---

## 1. Folder Creation Support

### Goals
- Expose a safe API for creating nested folders under the user’s assets root.
- Extend the web UI so users can create folders without leaving the browser.
- Guard against directory traversal, duplicates, and disallowed characters.

### API Changes
- **Endpoint:** `POST /assets/folders`
  - **Payload:** `{ "path": "covers/night" }` (interpreted as relative to the assets root).
  - **Validation:**
    - Reject empty or whitespace-only names.
    - Reject path segments containing `..`, path separators, or control characters.
    - Limit total depth (e.g., max 8 segments) to discourage abuse.
  - **Behaviour:**
    - Resolve against `ConfigPaths.assets_dir` respecting symlink/security checks.
    - Use `mkdir(parents=True, exist_ok=False)` to surface duplicate errors cleanly.
  - **Responses:**
    - `201 Created` with JSON payload including the canonical path and metadata.
    - `400 Bad Request` for validation failures.
    - `409 Conflict` when the folder already exists.
    - `500` for unexpected filesystem errors (log with context).
- Add unit tests covering happy-path creation, duplicate detection, and invalid names.
- Consider rate-limiting or authentication hooks if multi-user deployment is expected.

### UI Work
- Add a “New Folder” button or menu item in the assets pane.
  - Open a modal/dialog prompting for the folder name (optionally the parent path if not implied by the current view).
  - Call the new API endpoint; show inline validation errors returned by the backend.
  - Refresh the asset listing on success and optionally auto-focus the new folder.
- Handle busy state/spinners so users receive feedback while the request is running.
- Add empty-state messaging encouraging folder creation when a directory has no subfolders.

### Edge Cases & Validation
- Ensure the UI prevents submission of empty strings or names containing `/` or `\`.
- Decide whether to automatically sanitize leading/trailing spaces or reject them outright.
- Confirm behaviour when the current directory is read-only or missing (fail gracefully with an error toast).

### Testing Strategy
- New API tests (unit + integration) using a temporary filesystem.
- UI smoke tests (manual or automated) verifying creation, error handling, and refresh behaviour.
- Regression test to confirm existing read-only listing still works.

### Rollout Notes
- Update API docs and README to describe the new endpoint and UI capability.
- Communicate to users that destructive actions (rename/delete) remain out of scope for now.

---

## 1b. Uploads Targeting Specific Folders

### Goals
- Let users choose a destination directory before uploading files.
- Fail gracefully if the selected directory goes stale.

### API Changes
- Reuse `POST /config/assets` with a stricter `target_dir` contract:
  - Optionally add `auto_create` to control whether missing directories should be created or rejected with `404`.
  - Include the resolved folder in responses so the UI can refresh that branch only.
- Add tests covering uploads into nested folders, missing directories, and the auto-create toggle.

### UI Work
- Surface a folder selector (drop-down or tree) alongside the Upload button.
- Default to the folder that the user last expanded/selected in the asset tree.
- Pass `target_dir` when uploading; on success, refresh the affected branch and show a toast.
- Display backend errors if the folder no longer exists.

### Testing
- Manual verification uploading to root and nested directories.
- Automated integration test if/when UI test harness is available.

---

## 1c. Move / Rename Assets

### Goals
- Allow users to move or rename assets without re-uploading.

### API Changes
- Introduce `POST /config/assets/move` with payload `{ "source": "covers/night.jpg", "destination": "hero/night.jpg" }`.
  - Validate source/destination, prevent traversal, and optionally allow overwriting via a flag.
  - Return updated metadata for the moved asset.
- Tests: happy path, destination exists (409), missing source (404), invalid paths (400).

### UI Work
- Add a “Move / Rename” action in the asset row.
- Modal dialog to pick the target folder (reuse selector) and adjust the filename.
- Refresh the tree, highlight the new location, and display a toast on success.

### Testing
- Manual scenarios for move, rename, cancellation, and error handling.

---

## 1d. Delete Folders (Recursive)

### Goals
- Let users delete folders (optionally with contents) from the UI.

### API Changes
- Add `DELETE /config/assets/{folder_path}` supporting `?recursive=true`.
  - Require the flag for non-empty folders (otherwise return `409`).
  - Validate that the target is a directory under the assets root and not the root itself.
- Tests for empty folder delete, recursive delete, missing folder, invalid path.

### UI Work
- Provide a “Delete Folder” action (context menu or button).
- Confirmation dialog explaining that contents will be removed.
- Refresh the asset tree and collapse any removed branches.

### Testing
- Manual checks deleting empty/non-empty folders and ensuring errors show correctly.

---

## 2. Image Hover Preview

### Goals
- Provide quick visual confirmation of image assets from the listing without downloading them.
- Keep bandwidth reasonable by reusing existing static file serving.

### API & Backend Considerations
- No new endpoints required if the existing static file handler (FastAPI `StaticFiles`) already serves `/assets/...` paths.
- If direct access isn’t available, expose a lightweight `GET /assets/preview?path=...` endpoint returning an image or base64 payload with caching headers.
- Enforce access control so only assets under the configured root are fetchable.

### UI Implementation
- On hover of a file row with an image extension (`.jpg`, `.jpeg`, `.png`, `.gif`, `.webp`):
  - Display a tooltip/popover containing a small preview (e.g., 200×200 max) and basic metadata (dimensions, size if available).
  - Lazy-load the image the first time the tooltip opens to avoid preloading every thumbnail.
- Consider keyboard accessibility: allow focusing the row and showing the preview via keyboard interaction.
- Provide fallback text (e.g., “Preview unavailable”) for unsupported formats or load errors.

### Performance & Caching
- Use browser caching by pointing `<img>` to the existing asset URL; rely on HTTP headers for cache control.
- Debounce tooltip show/hide to avoid flapping when users move the cursor quickly.
- Ensure large images don’t distort the UI—constrain via CSS and maybe request scaled variants later if necessary.

### Testing Strategy
- Cross-browser manual checks (Chrome, Firefox, Safari) for tooltip behaviour.
- Verify that previews respect authentication/session requirements.
- Regression check that non-image files still show their standard tooltip or none at all.

### Future Considerations
- Potentially add right-click context menus (rename/delete) once folder creation proves stable.
- Offer thumbnail previews directly in the list (column view) if hover UX receives positive feedback.

---

## Dependencies & Open Questions
- **Authentication:** Does the web UI require auth headers for static asset URLs? Confirm before relying on direct links.
- **Rate Limiting:** Should the folder creation endpoint be throttled to prevent abuse?
- **Error Logging:** Ensure server-side errors include the requested path and user context.
- **Internationalization:** Decide if folder name validation should allow non-ASCII characters.

---

## Next Steps
1. Implement and test the `POST /assets/folders` endpoint.
2. Update the assets UI with the new folder creation workflow.
3. Extend uploads so users can target specific folders.
4. Add move/rename capability (API + UI).
5. Implement recursive folder deletion with confirmation UX.
6. Add hover preview tooling, starting with frontend changes and validating backend accessibility.
7. Document the new capabilities in README/API docs and announce to users.
