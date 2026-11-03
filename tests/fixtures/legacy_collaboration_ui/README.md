# Legacy collaboration UI test snapshot

These twelve JavaScript and CSS files preserve the exact bytes from commit
`a60d43c36486821273343453f5b6f5b7780ac833`. Their sizes and SHA-256 identities are
recorded in `manifest.json`.

They are test material outside the production static root. Full browser journeys
use `tests.legacy_collaboration_ui` to intercept requests in that disposable page
only; component tests load these files directly. No application route serves the
fixture prefix. HTTP actions, subscriptions and operation receipts in integration
journeys continue to use the real retained backend.

The production entry uses `collaboration-room.js`, `collaboration-room-store.js`
and `collaboration-room.css`. Moving this UI snapshot does not retire the old wire
protocol or prove external hosts have migrated. Usage evidence, replacement
journeys and an explicit retirement record remain separate release decisions.
