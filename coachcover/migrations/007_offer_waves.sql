-- Schema 7: offers go out in waves (product decision). Several offers of one case can be open at once; a "yes" is
-- held for a short window so that a person higher in the queue can still take the shift.
DROP INDEX offer_one_pending_per_case;
ALTER TABLE offer ADD COLUMN wave smallint NOT NULL DEFAULT 1 CHECK (wave >= 1);
ALTER TABLE offer DROP CONSTRAINT offer_status_check;
ALTER TABLE offer ADD CONSTRAINT offer_status_check
    CHECK (status IN ('pending', 'held', 'accepted', 'declined', 'expired', 'withdrawn', 'released'));
ALTER TABLE offer ADD CONSTRAINT offer_held_answered CHECK (status <> 'held' OR responded_at IS NOT NULL);
CREATE INDEX offer_case_active ON offer (case_id) WHERE status IN ('pending', 'held');
