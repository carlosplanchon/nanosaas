-- Hashes the API keys stored before NanoSaaS kept only their hash, the same
-- way Account.hash_api_key() does: "first 8 characters$SHA-256 in hex".
-- The keys keep working, so nobody has to generate a new one.
--
-- Run it once, with the app stopped, before starting the version that hashes
-- the keys. Running it again is harmless: hashed values have a "$", and the
-- keys themselves are hexadecimal. Needs PostgreSQL 11 or newer.
UPDATE account
SET api_key = left(api_key, 8) || '$' || encode(sha256(convert_to(api_key, 'UTF8')), 'hex')
WHERE api_key IS NOT NULL AND position('$' IN api_key) = 0;
