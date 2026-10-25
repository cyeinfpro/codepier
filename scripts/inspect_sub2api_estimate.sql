-- Read-only aggregate audit. No credentials, prompts, API keys or session IDs leave PostgreSQL.
-- Run with psql -X -qAt -v window_start='2026-09-25 15:40+08'
--   -v holdout_start='2026-10-18 15:40+08' -v window_end='2026-10-25 15:40+08' -f this_file
\set ON_ERROR_STOP on
BEGIN TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY;
SET LOCAL statement_timeout = '30s';
WITH
bounds AS (
  SELECT :'window_start'::timestamptz AS start_at,
         :'holdout_start'::timestamptz AS holdout_at,
         :'window_end'::timestamptz AS end_at
),
u AS (
  SELECT l.input_tokens::bigint AS uncached, l.cache_read_tokens::bigint AS cached,
         l.cache_creation_tokens::bigint AS writes, l.output_tokens::bigint AS output,
         l.input_tokens::bigint+l.cache_read_tokens+l.cache_creation_tokens AS full_input,
         l.total_cost AS list_usd, l.input_cost, l.output_cost, l.cache_read_cost,
         l.cache_creation_cost, l.created_at, l.id, l.api_key_id, l.session_id,
         l.service_tier, l.reasoning_effort, l.long_context_billing_applied,
         l.upstream_request_id, l.native_compaction_v2, l.upstream_model_mismatch,
         CASE WHEN l.user_agent ILIKE '%codex%' THEN 'codex_client'
              WHEN l.user_agent ILIKE 'FrameFlow/%' THEN 'frameflow'
              WHEN l.user_agent ILIKE 'python-httpx/%' THEN 'python_httpx'
              ELSE 'other' END AS cohort,
         CASE WHEN l.created_at < b.holdout_at THEN 'train_23d' ELSE 'holdout_7d' END AS period
  FROM usage_logs l CROSS JOIN bounds b
  WHERE l.model='gpt-6-astra' AND l.created_at>=b.start_at AND l.created_at<b.end_at
),
cohorts AS (
  SELECT cohort, count(*) AS calls, count(*) FILTER(WHERE full_input+output>0) AS token_rows,
         sum(uncached) AS uncached, sum(cached) AS cached, sum(writes) AS writes,
         sum(full_input) AS full_input, sum(output) AS output, sum(list_usd) AS list_usd,
         round(avg(full_input),4) AS mean_input,
         round(avg(full_input) FILTER(WHERE full_input+output>0),4) AS mean_input_token_rows,
         round(avg(output),4) AS mean_output,
         round(avg(output) FILTER(WHERE full_input+output>0),4) AS mean_output_token_rows,
         percentile_cont(ARRAY[.1,.5,.9,.95,.99]) WITHIN GROUP(ORDER BY full_input) AS input_quantiles,
         percentile_cont(ARRAY[.1,.5,.9,.95,.99]) WITHIN GROUP(ORDER BY output) AS output_quantiles,
         100.0*sum(cached)/nullif(sum(full_input),0) AS weighted_cache_percent,
         sum(list_usd)/count(*) AS usd_per_call,
         sum(full_input::numeric*1.45/1000000+output::numeric*50/1000000) AS known_tokens_fixed95_usd
  FROM u GROUP BY cohort
),
periods AS (
  SELECT period, count(*) AS calls,
         count(*) FILTER(WHERE full_input+output>0) AS token_rows,
         sum(full_input) AS full_input, sum(output) AS output, sum(list_usd) AS list_usd,
         avg(full_input) FILTER(WHERE full_input+output>0) AS mean_input_token_rows,
         avg(output) FILTER(WHERE full_input+output>0) AS mean_output_token_rows,
         100.0*sum(cached)/nullif(sum(full_input),0) AS weighted_cache_percent,
         sum(full_input::numeric*1.45/1000000+output::numeric*50/1000000) AS known_tokens_fixed95_usd,
         sum(least(full_input,64000)::numeric*1.45/1000000+output::numeric*50/1000000) AS sensitivity_64k_usd
  FROM u WHERE cohort='codex_client' GROUP BY period
),
ranked AS (
  SELECT *, row_number() OVER(PARTITION BY api_key_id,session_id ORDER BY created_at,id) AS seq
  FROM u WHERE cohort='codex_client' AND nullif(session_id,'') IS NOT NULL AND full_input>0
),
phases AS (
  SELECT CASE WHEN seq=1 THEN 'first_observed_in_window' ELSE 'later_in_session' END AS phase,
         count(*) AS calls, avg(full_input) AS mean_input, avg(output) AS mean_output,
         100.0*sum(cached)/nullif(sum(full_input),0) AS weighted_cache_percent
  FROM ranked GROUP BY 1
),
bucketed AS (
  SELECT *, CASE WHEN full_input=0 THEN 'no_input' WHEN full_input<=32000 THEN '0_32k'
            WHEN full_input<=64000 THEN '32_64k' WHEN full_input<=128000 THEN '64_128k'
            WHEN full_input<=200000 THEN '128_200k' ELSE '200k_plus' END AS bucket
  FROM u WHERE cohort='codex_client'
),
buckets AS (
  SELECT bucket, count(*) AS calls, sum(list_usd) AS list_usd FROM bucketed GROUP BY bucket
),
tiers AS (
  SELECT coalesce(nullif(service_tier,''),'unspecified') AS tier, count(*) AS calls,
         sum(input_cost)*1000000/nullif(sum(uncached),0) AS input_usd_per_million,
         sum(cache_read_cost)*1000000/nullif(sum(cached),0) AS cache_usd_per_million,
         sum(cache_creation_cost)*1000000/nullif(sum(writes),0) AS write_usd_per_million,
         sum(output_cost)*1000000/nullif(sum(output),0) AS output_usd_per_million,
         sum(list_usd) AS list_usd
  FROM u WHERE full_input+output>0 GROUP BY 1
),
efforts AS (
  SELECT reasoning_effort AS effort, count(*) AS calls, avg(full_input) AS mean_input,
         avg(output) AS mean_output, percentile_cont(.95) WITHIN GROUP(ORDER BY output) AS output_p95,
         100.0*sum(cached)/nullif(sum(full_input),0) AS weighted_cache_percent,
         sum(list_usd)/count(*) AS usd_per_call
  FROM u WHERE cohort='codex_client' AND full_input+output>0 GROUP BY reasoning_effort
)
SELECT json_build_object(
  'schema_version',1,
  'source','Sub2API usage_logs; read-only aggregate snapshot',
  'model','gpt-6-astra',
  'scope','all gateway clients; no proven CodePier-project/request correlation',
  'cost_basis','total_cost list USD; no account or group multipliers',
  'window_start',(SELECT start_at FROM bounds),
  'holdout_start',(SELECT holdout_at FROM bounds),
  'window_end_exclusive',(SELECT end_at FROM bounds),
  'quantile_probabilities',ARRAY[.1,.5,.9,.95,.99],
  'all',(SELECT json_build_object(
    'calls',count(*),'uncached',sum(uncached),'cached',sum(cached),'writes',sum(writes),
    'full_input',sum(full_input),'output',sum(output),'list_usd',sum(list_usd),
    'input_cost',sum(input_cost),'cache_read_cost',sum(cache_read_cost),
    'cache_creation_cost',sum(cache_creation_cost),'output_cost',sum(output_cost),
    'zero_token_rows',count(*) FILTER(WHERE full_input+output=0),
    'priced_zero_token_rows',count(*) FILTER(WHERE full_input+output=0 AND list_usd>0),
    'zero_token_list_usd',sum(list_usd) FILTER(WHERE full_input+output=0),
    'component_mismatch_rows',count(*) FILTER(WHERE abs(list_usd-input_cost-output_cost-cache_read_cost-cache_creation_cost)>.000001),
    'above_272k_rows',count(*) FILTER(WHERE full_input>272000),
    'long_context_billed_rows',count(*) FILTER(WHERE long_context_billing_applied),
    'max_input',max(full_input),'max_output',max(output),
    'cache_write_rows',count(*) FILTER(WHERE writes>0)
  ) FROM u),
  'cohorts',(SELECT json_agg(c ORDER BY cohort) FROM cohorts c),
  'codex_validation',(SELECT json_agg(p ORDER BY period) FROM periods p),
  'codex_session_phases',(SELECT json_agg(p ORDER BY phase) FROM phases p),
  'codex_input_buckets',(SELECT json_agg(b ORDER BY bucket) FROM buckets b),
  'observed_tier_rates',(SELECT json_agg(t ORDER BY tier) FROM tiers t),
  'codex_reasoning_efforts',(SELECT json_agg(e ORDER BY effort) FROM efforts e),
  'codex_correlation',(SELECT json_build_object(
    'calls',count(*),
    'session_rows',count(*) FILTER(WHERE nullif(session_id,'') IS NOT NULL),
    'distinct_key_session_pairs',count(DISTINCT (api_key_id,session_id)) FILTER(WHERE nullif(session_id,'') IS NOT NULL),
    'upstream_request_id_rows',count(*) FILTER(WHERE nullif(upstream_request_id,'') IS NOT NULL),
    'native_compaction_rows',count(*) FILTER(WHERE native_compaction_v2),
    'upstream_model_mismatch_rows',count(*) FILTER(WHERE upstream_model_mismatch)
  ) FROM u WHERE cohort='codex_client')
);
COMMIT;
