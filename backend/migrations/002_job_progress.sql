-- How far a running job has got (step, section, chunks done), for the UI.
alter table jobs add column progress jsonb;
