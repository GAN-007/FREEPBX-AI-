# Security Policy

## Supported Versions

We actively support the following versions with security updates:

| Version | Supported          | End of Support |
| ------- | ------------------ | -------------- |
| 4.1.x   | :white_check_mark: | TBD            |
| 4.0.x   | :white_check_mark: | TBD            |
| < 4.0   | :x:                | Ended          |

**Recommendation**: Always use the latest v4.1.x release for the most recent security patches and features.

---

## Reporting a Vulnerability

We take security vulnerabilities seriously. If you discover a security issue, please follow our responsible disclosure process.

### Reporting Process

1. **DO NOT** open a public GitHub issue for security vulnerabilities
2. **Email** security reports to: support@jugaar.llc
3. **Include** the following in your report:
   - Description of the vulnerability
   - Steps to reproduce the issue
   - Affected versions
   - Potential impact
   - Any suggested fixes (optional)

### What to Expect

| Timeline | Action |
|----------|--------|
| **Within 48 hours** | We will acknowledge receipt of your report |
| **Within 7 days** | We will provide an initial assessment and timeline |
| **Within 30 days** | We will aim to release a patch or mitigation guidance |

### Response SLAs

- **Critical vulnerabilities** (remote code execution, authentication bypass):
  - Acknowledgment: 24 hours
  - Patch release: 7-14 days
  
- **High vulnerabilities** (privilege escalation, data exposure):
  - Acknowledgment: 48 hours
  - Patch release: 14-30 days
  
- **Medium/Low vulnerabilities**:
  - Acknowledgment: 72 hours
  - Patch release: Next scheduled release

---

## Security Best Practices

### 1. Credentials Management

**CRITICAL**: Never commit credentials to version control.

- Store all secrets in `.env` file (gitignored by default)
- Required secrets:
  ```bash
  ASTERISK_ARI_USERNAME=your_username
  ASTERISK_ARI_PASSWORD=your_secure_password
  OPENAI_API_KEY=sk-...
  DEEPGRAM_API_KEY=...
  ```
- Use strong, unique passwords (minimum 16 characters)
- Rotate API keys every 90 days
- Never include `.env` in Docker images

### 2. Network Security

**Default Configuration** (Secure):
- RTP Server: Binds to `127.0.0.1:18080` (localhost only)
- AudioSocket: Binds to `127.0.0.1:8090` (localhost only)
- Health Endpoint: Binds to `127.0.0.1:15000` (localhost only)

**If Remote Access Required**:
```bash
# .env file
EXTERNAL_MEDIA_RTP_HOST=0.0.0.0  # Explicit opt-in
HEALTH_BIND_HOST=0.0.0.0         # Explicit opt-in
```

**Firewall Rules** (if binding to 0.0.0.0):
```bash
# Only allow from Asterisk server
sudo ufw allow from 10.0.1.5 to any port 18080  # RTP
sudo ufw allow from 10.0.1.5 to any port 8090   # AudioSocket
```

### 3. Docker Security

**Run as Non-Root**:
- Containers run as `appuser` (non-root) by default
- Never override this with `user: root`

**Keep Base Images Updated**:
```bash
# Check for updates
docker pull python:3.11@sha256:e8ab764baee5109566456913b42d7d4ad97c13385e4002973c896e1dd5f01146

# Rebuild
docker compose build --no-cache
```

### 4. Dependency Security

**Automated Scanning** (enabled via CI):
- **Dependabot**: Weekly dependency updates
- **Trivy**: Docker vulnerability scanning
- **CodeQL**: Static code analysis

**Manual Checks**:
```bash
# Check Python dependencies
pip list --outdated

# Scan for known vulnerabilities
pip-audit
```

### 5. Logging Security

**Log Sanitization** (automatic):
- API keys, passwords, tokens automatically redacted in logs
- Example: `api_key: "sk***REDACTED***"`
- Implemented via structlog processor (AAVA-37)

**Log Access Control**:
```bash
# Restrict log file permissions
chmod 640 logs/*.log
chown appuser:appgroup logs/*.log
```

### 6. Production Hardening

**Required for Production**:
- [ ] Change default credentials
- [ ] Enable firewall (ufw/iptables)
- [ ] Configure log rotation
- [ ] Enable monitoring/alerting
- [ ] Regular backup schedule
- [ ] TLS/SSL for external access
- [ ] Rate limiting for API endpoints

**Environment Variables**:
```bash
# Production settings
LOG_LEVEL=info              # Not debug (security risk)
STREAMING_LOG_LEVEL=info    # Not debug (performance impact)
```

---

## Known Security Considerations

### 1. API Provider Dependencies

This application integrates with third-party AI providers:
- **OpenAI**: Processes audio/text via their API
- **Deepgram**: Processes audio via their API
- **Google**: (If configured) Processes audio via their API

**Privacy Implications**:
- Audio is transmitted to cloud providers for processing
- Review provider privacy policies and DPAs
- For complete data privacy, use `local_only` configuration

### 2. Asterisk Integration

**ARI Credentials**:
- Requires Asterisk ARI username/password
- Use dedicated ARI user (not `admin`)
- Grant only necessary permissions
- Example `/etc/asterisk/ari.conf`:
  ```ini
  [AIAgent]
  type=user
  read_only=no
  password=strong_random_password_here
  ```

### 3. Audio File Storage

**Local Hybrid Configuration**:
- Audio files stored in `/mnt/asterisk_media/ai-generated/`
- Contains TTS audio (may include sensitive information)
- Recommendations:
  - Set appropriate filesystem permissions (750)
  - Configure file retention policy
  - Encrypt volume if required by compliance

---

## Compliance

### HIPAA (Healthcare)

If processing Protected Health Information (PHI):
- [ ] Enable audit logging
- [ ] Encrypt audio files at rest
- [ ] Sign Business Associate Agreements (BAAs) with AI providers
- [ ] Implement access controls
- [ ] Configure log retention per requirements

### GDPR (EU Personal Data)

If processing EU personal data:
- [ ] Implement data retention policies
- [ ] Provide data deletion procedures
- [ ] Document data processing activities
- [ ] Obtain necessary consents
- [ ] Review AI provider GDPR compliance

---

## Security Updates

Subscribe to security notifications:
- **GitHub Watch**: Enable "Releases only" notifications
- **Security Advisories**: Check [GitHub Security tab](https://github.com/hkjarral/Asterisk-AI-Voice-Agent/security)
- **Dependabot Alerts**: Review weekly PR updates

---

## Disclosure Policy

### Coordinated Disclosure

We follow coordinated disclosure practices:
1. Reporter notifies us privately
2. We develop and test a fix
3. We release a security patch
4. Public disclosure after patch is available
5. Credit given to reporter (if desired)

### Public Disclosure Timeline

- **Typical**: 30-90 days after initial report
- **May be extended** if fix is complex or requires coordination
- **May be shortened** if exploit is public or actively used

---

## Security Contact

For security-related questions or concerns:
- **Security Reports**: [Your security email]
- **General Security Questions**: Open a GitHub Discussion
- **Emergency**: Tag issue with `security` label (for non-sensitive issues only)

---

## Acknowledgments

We thank the security research community for responsible disclosure practices. Security researchers who have helped improve this project:

- [List of contributors who reported security issues]

---

## Version History

| Date | Version | Changes |
|------|---------|---------|
| 2025-11-07 | 1.0 | Initial security policy |

---

## Additional Resources

- [Production Deployment Guide](docs/PRODUCTION_DEPLOYMENT.md)
- [Configuration Reference](docs/Configuration-Reference.md)
- [Monitoring Guide](docs/MONITORING_GUIDE.md)
