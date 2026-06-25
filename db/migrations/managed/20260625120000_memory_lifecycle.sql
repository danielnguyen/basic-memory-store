ALTER TABLE memory_items
  DROP CONSTRAINT IF EXISTS memory_items_status_check;

ALTER TABLE memory_items
  ADD CONSTRAINT memory_items_status_check CHECK (status IN (
    'active',
    'parked',
    'stale',
    'contradicted',
    'corrected',
    'invalidated',
    'superseded',
    'expired',
    'retracted',
    'forgotten_or_demoted',
    'rebuilding'
  ));

ALTER TABLE memory_events
  DROP CONSTRAINT IF EXISTS memory_events_event_type_check;

ALTER TABLE memory_events
  ADD CONSTRAINT memory_events_event_type_check CHECK (event_type IN (
    'created',
    'updated',
    'reinforced',
    'superseded',
    'expired',
    'promoted',
    'suppressed',
    'decayed',
    'state_changed'
  ));
