-- Runs once, when the Postgres container first initialises its data directory.
-- Creates the database that pytest points at. Keeping tests in a separate
-- database (rather than a separate schema, or rolling back against `pina`)
-- means a test run can never clobber the places you saved by hand.
CREATE DATABASE pina_test OWNER pina;
