# Workspace output width fix

## Scope and acceptance

Fill the available workspace content width on `/workspace-files`, retaining the
shared page padding and existing search, list, and download behavior. No file
management, backend, storage, or billing changes are included.

The cause was a 960px maximum on `.workspace-files-content`. The result container
now uses 100% width. The design system explicitly distinguishes full-width result
lists from the narrower search field.

## Verification

- `npm run build`: passed, including TypeScript.
- Playwright: 1920, 1440, 700, and 390px, populated, empty, and error states.
- Each result container matches the parent content width; no horizontal overflow.
- Desktop and mobile screenshots visually inspected, including long filenames.
- Screenshots and isolated fixture script remain in `output/`.

## Deployment

The first deployment incorrectly used the obsolete `/data1/rag-platform` source
directory. It caused a temporary 404 and was reverted to the original image
`7d139e2289f9`; the endpoint returned HTTP 200 again. The active release is
`/data1/rag-releases/storage72/source`, using both Compose files and the
`rag-platform` project. Future releases must inspect the running container's
Compose configuration and verify the active release before building. The fix is
applied as a single CSS replacement in that release, with a source backup and
the original image tagged `rag-platform-web:before-output-width`.

The corrected release built and deployed successfully. The live page returned
HTTP 200, displayed all six existing outputs, and measured 1616px for both the
available content width and result container. Download links remained present.
