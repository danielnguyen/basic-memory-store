-- Internal reverse binding; lifecycle remains owned by work_items.
ALTER TABLE messages ADD COLUMN IF NOT EXISTS work_id UUID;
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conrelid = 'messages'::regclass
                 AND conname = 'messages_work_id_fkey') THEN
    ALTER TABLE messages ADD CONSTRAINT messages_work_id_fkey
      FOREIGN KEY (work_id) REFERENCES work_items(work_id) ON DELETE CASCADE;
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conrelid = 'messages'::regclass
                 AND conname = 'messages_work_id_key') THEN
    ALTER TABLE messages ADD CONSTRAINT messages_work_id_key UNIQUE (work_id);
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conrelid = 'messages'::regclass
                 AND conname = 'messages_work_assistant_check') THEN
    ALTER TABLE messages ADD CONSTRAINT messages_work_assistant_check
      CHECK (work_id IS NULL OR role = 'assistant');
  END IF;
END $$;
