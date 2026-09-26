"""SQL run on the primary via psql; each returns a single jsonb value."""

# roles and connectable databases (run against the default database)
CLUSTER = """
SELECT jsonb_build_object(
  'databases', (SELECT coalesce(jsonb_agg(datname ORDER BY datname), '[]'::jsonb)
                FROM pg_database WHERE datallowconn AND NOT datistemplate),
  'roles', (SELECT coalesce(jsonb_agg(jsonb_build_object(
        'name', r.rolname,
        'super', r.rolsuper,
        'in_roles', (SELECT coalesce(jsonb_agg(g.rolname ORDER BY g.rolname), '[]'::jsonb)
                     FROM pg_auth_members m JOIN pg_roles g ON g.oid = m.roleid WHERE m.member = r.oid)
      ) ORDER BY r.rolname), '[]'::jsonb)
    FROM pg_roles r)
)
"""

# non-default grants in the current database, one row per privilege.
# Left out: owners' own privileges, PostgreSQL's built-in PUBLIC defaults
# (USAGE on schema public, CONNECT/TEMP on the database).
DB_ACL = """
SELECT jsonb_build_object(
  'schema_grants', (SELECT coalesce(jsonb_agg(DISTINCT jsonb_build_object(
        'schema', n.nspname, 'grantee', coalesce(gr.rolname, 'PUBLIC'),
        'privilege', x.privilege_type)), '[]'::jsonb)
      FROM pg_namespace n
      CROSS JOIN LATERAL aclexplode(n.nspacl) x
      LEFT JOIN pg_roles gr ON gr.oid = x.grantee
      WHERE n.nspname <> 'information_schema' AND n.nspname !~ '^pg_'
        AND x.grantee <> n.nspowner
        AND NOT (n.nspname = 'public' AND x.grantee = 0 AND x.privilege_type = 'USAGE')),
  'object_grants', (SELECT coalesce(jsonb_agg(DISTINCT jsonb_build_object(
        'grantee', coalesce(gr.rolname, 'PUBLIC'), 'schema', n.nspname, 'object', c.relname,
        'kind', CASE WHEN c.relkind = 'S' THEN 'sequence' ELSE 'table' END,
        'privilege', x.privilege_type)), '[]'::jsonb)
      FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
      CROSS JOIN LATERAL aclexplode(c.relacl) x
      LEFT JOIN pg_roles gr ON gr.oid = x.grantee
      WHERE c.relkind IN ('r','p','v','m','f','S')
        AND n.nspname <> 'information_schema' AND n.nspname !~ '^pg_'
        AND x.grantee <> c.relowner
        AND NOT (x.grantee = 0 AND x.privilege_type = 'SELECT'
                 AND c.relname IN ('pg_stat_statements', 'pg_stat_statements_info'))),
  -- per-schema entries hold only additions; a global entry holds the full
  -- ACL, so drop what acldefault() already gives (owner, PUBLIC EXECUTE/USAGE)
  'default_acls', (SELECT coalesce(jsonb_agg(DISTINCT jsonb_build_object(
        'grantor', ro.rolname, 'schema', coalesce(n.nspname, ''),
        'kind', CASE d.defaclobjtype WHEN 'r' THEN 'TABLES' WHEN 'S' THEN 'SEQUENCES'
                                     WHEN 'f' THEN 'FUNCTIONS' WHEN 'T' THEN 'TYPES'
                                     WHEN 'n' THEN 'SCHEMAS' WHEN 'L' THEN 'LARGE OBJECTS' END,
        'grantee', coalesce(gr.rolname, 'PUBLIC'), 'privilege', x.privilege_type)), '[]'::jsonb)
      FROM pg_default_acl d
      JOIN pg_roles ro ON ro.oid = d.defaclrole
      LEFT JOIN pg_namespace n ON n.oid = d.defaclnamespace
      CROSS JOIN LATERAL aclexplode(d.defaclacl) x
      LEFT JOIN pg_roles gr ON gr.oid = x.grantee
      WHERE d.defaclnamespace <> 0
         OR (x.grantee, x.privilege_type) NOT IN (
              SELECT y.grantee, y.privilege_type FROM aclexplode(acldefault(
                CASE d.defaclobjtype WHEN 'S' THEN 's' ELSE d.defaclobjtype END,
                d.defaclrole)) y)),
  'db_grants', (SELECT coalesce(jsonb_agg(DISTINCT jsonb_build_object(
        'database', d.datname, 'grantee', coalesce(gr.rolname, 'PUBLIC'),
        'privilege', x.privilege_type)), '[]'::jsonb)
      FROM pg_database d
      CROSS JOIN LATERAL aclexplode(d.datacl) x
      LEFT JOIN pg_roles gr ON gr.oid = x.grantee
      WHERE d.datname = current_database()
        AND x.grantee <> d.datdba
        AND NOT (x.grantee = 0 AND x.privilege_type IN ('CONNECT', 'TEMPORARY')))
)
"""

# relations of the current database (with partition parent) and the keywords
# quote_ident() quotes
CATALOG = """
SELECT jsonb_build_object(
  'objects', (SELECT coalesce(jsonb_agg(jsonb_build_object(
        'schema', n.nspname, 'name', c.relname,
        'kind', CASE WHEN c.relkind = 'S' THEN 'sequence' ELSE 'table' END,
        'parent_schema', pn.nspname, 'parent', p.relname)), '[]'::jsonb)
      FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
      LEFT JOIN pg_inherits i ON i.inhrelid = c.oid AND c.relispartition
      LEFT JOIN pg_class p ON p.oid = i.inhparent
      LEFT JOIN pg_namespace pn ON pn.oid = p.relnamespace
      WHERE c.relkind IN ('r','p','v','m','f','S')
        AND n.nspname <> 'information_schema' AND n.nspname !~ '^pg_'),
  'keywords', (SELECT coalesce(jsonb_agg(word), '[]'::jsonb)
               FROM pg_get_keywords() WHERE catcode <> 'U')
)
"""
