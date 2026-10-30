-- Schema of the triage config tables as deployed: pg_dump --schema-only of
-- triage_config_versions, triage_chains, triage_rules, triage_email_mappings
-- and triage_email_mappings_history, plus their trigger functions. The
-- schema is owned by cortex-triage (ensure_config_schema) and postmark
-- (migrations/002); this copy lets the importer be tested against the real
-- triggers. Captured 2026-10-15. Needs btree_gist (only_one_active is an
-- EXCLUDE over a boolean); load with public on the search_path.

CREATE EXTENSION IF NOT EXISTS btree_gist SCHEMA public;

CREATE OR REPLACE FUNCTION increment_rule_version()
 RETURNS trigger
 LANGUAGE plpgsql
AS $function$
BEGIN
    NEW.row_version = OLD.row_version + 1;
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$function$
;
CREATE OR REPLACE FUNCTION bump_rule_row_version()
 RETURNS trigger
 LANGUAGE plpgsql
AS $function$
BEGIN
    NEW.row_version = OLD.row_version + 1;
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$function$
;
CREATE OR REPLACE FUNCTION log_email_mapping_change()
 RETURNS trigger
 LANGUAGE plpgsql
AS $function$
BEGIN
    IF TG_OP = 'INSERT' THEN
        INSERT INTO triage_email_mappings_history (
            mapping_id, mapping_type, email_address, label, archive, mark_read,
            change_type, changed_by
        ) VALUES (
            NEW.id, NEW.mapping_type, NEW.email_address, NEW.label, NEW.archive, NEW.mark_read,
            'created', COALESCE(NEW.created_by, 'unknown')
        );
        RETURN NEW;

    ELSIF TG_OP = 'UPDATE' THEN
        -- Only log if something actually changed (not just timestamp updates)
        IF (OLD.label IS DISTINCT FROM NEW.label) OR
           (OLD.archive IS DISTINCT FROM NEW.archive) OR
           (OLD.mark_read IS DISTINCT FROM NEW.mark_read) OR
           (OLD.deleted_at IS DISTINCT FROM NEW.deleted_at) THEN

            INSERT INTO triage_email_mappings_history (
                mapping_id, mapping_type, email_address, label, archive, mark_read,
                change_type, changed_by,
                previous_label, previous_archive, previous_mark_read
            ) VALUES (
                NEW.id, NEW.mapping_type, NEW.email_address, NEW.label, NEW.archive, NEW.mark_read,
                CASE WHEN NEW.deleted_at IS NOT NULL THEN 'deleted' ELSE 'updated' END,
                COALESCE(NEW.updated_by, NEW.created_by, 'unknown'),
                OLD.label, OLD.archive, OLD.mark_read
            );
        END IF;
        RETURN NEW;

    ELSIF TG_OP = 'DELETE' THEN
        INSERT INTO triage_email_mappings_history (
            mapping_id, mapping_type, email_address, label, archive, mark_read,
            change_type, changed_by
        ) VALUES (
            OLD.id, OLD.mapping_type, OLD.email_address, OLD.label, OLD.archive, OLD.mark_read,
            'deleted', COALESCE(OLD.updated_by, OLD.created_by, 'unknown')
        );
        RETURN OLD;
    END IF;

    RETURN NULL;
END;
$function$
;
CREATE OR REPLACE FUNCTION assign_config_version()
 RETURNS trigger
 LANGUAGE plpgsql
AS $function$
                BEGIN
                    IF NEW.version IS NULL THEN
                        SELECT COALESCE(MAX(version), 0) + 1
                        INTO NEW.version
                        FROM triage_config_versions;
                    END IF;
                    RETURN NEW;
                END;
                $function$
;
CREATE OR REPLACE FUNCTION deactivate_old_config_versions()
 RETURNS trigger
 LANGUAGE plpgsql
AS $function$
                BEGIN
                    IF NEW.is_active = TRUE THEN
                        UPDATE triage_config_versions
                        SET is_active = FALSE
                        WHERE version != NEW.version AND is_active = TRUE;
                    END IF;
                    RETURN NEW;
                END;
                $function$
;
CREATE OR REPLACE FUNCTION deactivate_old_configs()
 RETURNS trigger
 LANGUAGE plpgsql
AS $function$
                BEGIN
                    IF NEW.is_active = TRUE THEN
                        UPDATE triage_config_versions
                        SET is_active = FALSE
                        WHERE id != NEW.id AND is_active = TRUE;
                    END IF;
                    RETURN NEW;
                END;
                $function$
;


CREATE TABLE triage_email_mappings_history (
    id bigint NOT NULL,
    mapping_id bigint,
    mapping_type text NOT NULL,
    email_address text NOT NULL,
    label text NOT NULL,
    archive boolean,
    mark_read boolean,
    change_type text NOT NULL,
    changed_at timestamp with time zone DEFAULT now() NOT NULL,
    changed_by text NOT NULL,
    previous_label text,
    previous_archive boolean,
    previous_mark_read boolean,
    CONSTRAINT triage_email_mappings_history_change_type_check CHECK ((change_type = ANY (ARRAY['created'::text, 'updated'::text, 'deleted'::text])))
);
CREATE SEQUENCE triage_email_mappings_history_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;
ALTER SEQUENCE triage_email_mappings_history_id_seq OWNED BY triage_email_mappings_history.id;
ALTER TABLE ONLY triage_email_mappings_history ALTER COLUMN id SET DEFAULT nextval('triage_email_mappings_history_id_seq'::regclass);
ALTER TABLE ONLY triage_email_mappings_history
    ADD CONSTRAINT triage_email_mappings_history_pkey PRIMARY KEY (id);
CREATE INDEX idx_mapping_history_changed ON triage_email_mappings_history USING btree (changed_at DESC);
CREATE INDEX idx_mapping_history_email ON triage_email_mappings_history USING btree (email_address);
CREATE INDEX idx_mapping_history_mapping ON triage_email_mappings_history USING btree (mapping_id);

CREATE TABLE triage_config_versions (
    id bigint NOT NULL,
    version integer NOT NULL,
    config_hash text NOT NULL,
    label_prefix text DEFAULT 'Cortex'::text NOT NULL,
    intents jsonb DEFAULT '{}'::jsonb NOT NULL,
    email_categories jsonb DEFAULT '{}'::jsonb NOT NULL,
    prompts jsonb DEFAULT '{}'::jsonb NOT NULL,
    body_extraction_prompts jsonb DEFAULT '{}'::jsonb NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    created_by text NOT NULL,
    notes text,
    is_active boolean DEFAULT false NOT NULL,
    rolled_back_from integer,
    default_label text DEFAULT 'Uncategorized'::text NOT NULL
);
COMMENT ON TABLE triage_config_versions IS 'Versioned configuration snapshots with full history';
COMMENT ON COLUMN triage_config_versions.config_hash IS 'SHA256 of YAML content for deduplication';
COMMENT ON COLUMN triage_config_versions.is_active IS 'Only one config can be active. Trigger ensures exclusivity.';
CREATE TABLE triage_chains (
    id bigint NOT NULL,
    config_version integer NOT NULL,
    chain_name text NOT NULL,
    display_order integer DEFAULT 0 NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    auto_run boolean DEFAULT false NOT NULL
);
COMMENT ON TABLE triage_chains IS 'Rule chain containers (firewall-style chains)';
CREATE SEQUENCE triage_chains_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;
ALTER SEQUENCE triage_chains_id_seq OWNED BY triage_chains.id;
CREATE SEQUENCE triage_config_versions_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;
ALTER SEQUENCE triage_config_versions_id_seq OWNED BY triage_config_versions.id;
CREATE TABLE triage_email_mappings (
    id bigint NOT NULL,
    mapping_type text NOT NULL,
    email_address text NOT NULL,
    label text NOT NULL,
    archive boolean,
    mark_read boolean,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    created_by text,
    updated_by text,
    deleted_at timestamp with time zone,
    CONSTRAINT triage_email_mappings_v2_mapping_type_check CHECK ((mapping_type = ANY (ARRAY['priority'::text, 'fallback'::text])))
);
CREATE SEQUENCE triage_email_mappings_v2_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;
ALTER SEQUENCE triage_email_mappings_v2_id_seq OWNED BY triage_email_mappings.id;
CREATE TABLE triage_rules (
    id bigint NOT NULL,
    chain_id bigint NOT NULL,
    config_version integer NOT NULL,
    prev_rule_id bigint,
    next_rule_id bigint,
    match_condition jsonb DEFAULT '{}'::jsonb NOT NULL,
    variables jsonb,
    action jsonb,
    jump_to_chain text,
    return_to_parent boolean DEFAULT false,
    llm_config jsonb,
    routes jsonb,
    rule_name text,
    description text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    row_version integer DEFAULT 1 NOT NULL,
    CONSTRAINT exactly_one_outcome CHECK (((((((action IS NOT NULL))::integer + ((jump_to_chain IS NOT NULL))::integer) + ((return_to_parent = true))::integer) + ((llm_config IS NOT NULL))::integer) = 1)),
    CONSTRAINT valid_pointers CHECK ((((prev_rule_id IS NULL) OR (prev_rule_id <> id)) AND ((next_rule_id IS NULL) OR (next_rule_id <> id))))
);
COMMENT ON TABLE triage_rules IS 'Individual rules stored as linked list for efficient editing';
COMMENT ON COLUMN triage_rules.prev_rule_id IS 'Previous rule in chain (NULL = head of list)';
COMMENT ON COLUMN triage_rules.next_rule_id IS 'Next rule in chain (NULL = tail of list)';
COMMENT ON COLUMN triage_rules.row_version IS 'Optimistic locking counter. Incremented on every UPDATE.';
CREATE SEQUENCE triage_rules_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;
ALTER SEQUENCE triage_rules_id_seq OWNED BY triage_rules.id;
ALTER TABLE ONLY triage_chains ALTER COLUMN id SET DEFAULT nextval('triage_chains_id_seq'::regclass);
ALTER TABLE ONLY triage_config_versions ALTER COLUMN id SET DEFAULT nextval('triage_config_versions_id_seq'::regclass);
ALTER TABLE ONLY triage_email_mappings ALTER COLUMN id SET DEFAULT nextval('triage_email_mappings_v2_id_seq'::regclass);
ALTER TABLE ONLY triage_rules ALTER COLUMN id SET DEFAULT nextval('triage_rules_id_seq'::regclass);
ALTER TABLE ONLY triage_config_versions
    ADD CONSTRAINT only_one_active EXCLUDE USING gist (is_active WITH =) WHERE ((is_active = true));
ALTER TABLE ONLY triage_chains
    ADD CONSTRAINT triage_chains_pkey PRIMARY KEY (id);
ALTER TABLE ONLY triage_config_versions
    ADD CONSTRAINT triage_config_versions_pkey PRIMARY KEY (id);
ALTER TABLE ONLY triage_config_versions
    ADD CONSTRAINT triage_config_versions_version_key UNIQUE (version);
ALTER TABLE ONLY triage_email_mappings
    ADD CONSTRAINT triage_email_mappings_v2_pkey PRIMARY KEY (id);
ALTER TABLE ONLY triage_rules
    ADD CONSTRAINT triage_rules_pkey PRIMARY KEY (id);
ALTER TABLE ONLY triage_chains
    ADD CONSTRAINT unique_chain UNIQUE (config_version, chain_name);
CREATE INDEX idx_chains_config ON triage_chains USING btree (config_version);
CREATE INDEX idx_chains_name ON triage_chains USING btree (config_version, chain_name);
CREATE INDEX idx_chains_version ON triage_chains USING btree (config_version);
CREATE INDEX idx_config_created_at ON triage_config_versions USING btree (created_at DESC);
CREATE INDEX idx_config_hash ON triage_config_versions USING btree (config_hash);
CREATE UNIQUE INDEX idx_config_only_one_active ON triage_config_versions USING btree (is_active) WHERE (is_active = true);
CREATE INDEX idx_config_versions_active ON triage_config_versions USING btree (version DESC) WHERE (is_active = true);
CREATE INDEX idx_config_versions_hash ON triage_config_versions USING btree (config_hash);
CREATE UNIQUE INDEX idx_email_mappings_active ON triage_email_mappings USING btree (mapping_type, email_address) WHERE (deleted_at IS NULL);
CREATE INDEX idx_email_mappings_email ON triage_email_mappings USING btree (email_address) WHERE (deleted_at IS NULL);
CREATE INDEX idx_email_mappings_lookup ON triage_email_mappings USING btree (mapping_type, email_address) WHERE (deleted_at IS NULL);
CREATE INDEX idx_email_mappings_type ON triage_email_mappings USING btree (mapping_type) WHERE (deleted_at IS NULL);
CREATE INDEX idx_rules_chain ON triage_rules USING btree (chain_id);
CREATE INDEX idx_rules_chain_head ON triage_rules USING btree (chain_id) WHERE (prev_rule_id IS NULL);
CREATE INDEX idx_rules_config ON triage_rules USING btree (config_version);
CREATE INDEX idx_rules_next ON triage_rules USING btree (next_rule_id);
CREATE INDEX idx_rules_prev ON triage_rules USING btree (prev_rule_id);
CREATE INDEX idx_rules_version ON triage_rules USING btree (config_version);
CREATE TRIGGER assign_config_version_trigger BEFORE INSERT ON triage_config_versions FOR EACH ROW EXECUTE FUNCTION assign_config_version();
CREATE TRIGGER deactivate_old_configs_trigger BEFORE INSERT OR UPDATE ON triage_config_versions FOR EACH ROW WHEN ((new.is_active = true)) EXECUTE FUNCTION deactivate_old_configs();
CREATE TRIGGER email_mapping_change_log AFTER INSERT OR DELETE OR UPDATE ON triage_email_mappings FOR EACH ROW EXECUTE FUNCTION log_email_mapping_change();
CREATE TRIGGER increment_rule_version_trigger BEFORE UPDATE ON triage_rules FOR EACH ROW EXECUTE FUNCTION increment_rule_version();
CREATE TRIGGER trg_bump_rule_version BEFORE UPDATE ON triage_rules FOR EACH ROW EXECUTE FUNCTION bump_rule_row_version();
CREATE TRIGGER trg_deactivate_old_configs BEFORE INSERT OR UPDATE ON triage_config_versions FOR EACH ROW WHEN ((new.is_active = true)) EXECUTE FUNCTION deactivate_old_config_versions();
ALTER TABLE ONLY triage_chains
    ADD CONSTRAINT triage_chains_config_version_fkey FOREIGN KEY (config_version) REFERENCES triage_config_versions(version) ON DELETE CASCADE;
ALTER TABLE ONLY triage_config_versions
    ADD CONSTRAINT triage_config_versions_rolled_back_from_fkey FOREIGN KEY (rolled_back_from) REFERENCES triage_config_versions(version);
ALTER TABLE ONLY triage_rules
    ADD CONSTRAINT triage_rules_chain_id_fkey FOREIGN KEY (chain_id) REFERENCES triage_chains(id) ON DELETE CASCADE;
ALTER TABLE ONLY triage_rules
    ADD CONSTRAINT triage_rules_config_version_fkey FOREIGN KEY (config_version) REFERENCES triage_config_versions(version) ON DELETE CASCADE;
ALTER TABLE ONLY triage_rules
    ADD CONSTRAINT triage_rules_next_rule_id_fkey FOREIGN KEY (next_rule_id) REFERENCES triage_rules(id) ON DELETE SET NULL;
ALTER TABLE ONLY triage_rules
    ADD CONSTRAINT triage_rules_prev_rule_id_fkey FOREIGN KEY (prev_rule_id) REFERENCES triage_rules(id) ON DELETE SET NULL;
