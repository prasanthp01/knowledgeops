# Permission policy

| Demo role | Groups |
|---|---|
| Staff | public, staff |
| Engineering | public, staff, engineering |
| Security | public, staff, security |
| Finance | public, staff, finance |

The public endpoint always uses `demo`; a visitor cannot submit a tenant identifier. All records are original fiction. Choosing Finance grants access only to the fictional Finance fixtures. Private calls use a separate server-configured tenant/role and a minimum 32-character token. Token provisioning/rotation is an operator responsibility; no SSO claim is made.

Groups are applied in the SQL query before passages leave storage for ranking. No denied document titles, excerpts, counts or scoring frequencies are exposed. Traces say exclusions are not disclosed. Validity and retention deadlines override scenario revision selection. Revoked/expired/quarantined documents never support answers.

Use `python -m knowledgeops.cli revoke --tenant demo --document-id travel-policy` to revoke all versions. This invalidates old result/event reads. For ACL changes, ingest a new immutable version; never edit production tables by hand. Permission tightening cannot erase material a browser already received, but prevents subsequent server reads. Cross-session and cross-tenant tests protect this boundary.

