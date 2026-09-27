-- Tier 1 atomic update: dedup + minute-bucket increment + returned vector.
-- All keys share one hash tag so the script stays single-slot under cluster
-- mode (design.md Tier 1 contract).
--
-- KEYS[1] dedup hash    rtdp:t1:{scope}:dedup:<entity>
-- KEYS[2] count hash    rtdp:t1:{scope}:count:<pan>
-- KEYS[3] amount hash   rtdp:t1:{scope}:amt:<pan>:<currency>
-- KEYS[4] vector hash   rtdp:t1:{scope}:vec:<entity>
-- ARGV[1] event_id      stable dedup identity (tenant+txn+revision)
-- ARGV[2] payload_digest
-- ARGV[3] minute_bucket (epoch seconds / 60)
-- ARGV[4] amount
-- ARGV[5] dedup_horizon_seconds
-- ARGV[6] count_window_minutes   (60)
-- ARGV[7] amount_window_minutes  (1440)
-- ARGV[8] accept_live_only       "1" rejects events outside horizon

local dedup_key  = KEYS[1]
local count_key  = KEYS[2]
local amt_key    = KEYS[3]
local vec_key    = KEYS[4]
local event_id   = ARGV[1]
local digest     = ARGV[2]
local bucket     = tonumber(ARGV[3])
local amount     = tonumber(ARGV[4])
local horizon_s  = tonumber(ARGV[5])
local count_win  = tonumber(ARGV[6])
local amt_win    = tonumber(ARGV[7])
local live_only  = ARGV[8]

local prior = redis.call('HGET', dedup_key, event_id)
if prior then
  local sep = string.find(prior, '|')
  local prior_digest = string.sub(prior, 1, sep - 1)
  if prior_digest ~= digest then
    return redis.error_reply('CONFLICT:' .. event_id)
  end
  -- Retried request: return the cached vector; do not recount.
  return redis.call('HGET', vec_key, event_id)
end

local now_bucket = math.floor(tonumber(redis.call('TIME')[1]) / 60)
if live_only == '1' and (bucket > now_bucket or bucket * 60 <
    (now_bucket * 60) - horizon_s) then
  return redis.error_reply('OUT_OF_HORIZON:' .. event_id)
end

redis.call('HINCRBY', count_key, 'b:' .. bucket, 1)
redis.call('HINCRBYFLOAT', amt_key, 'b:' .. bucket, amount)
redis.call('HEXPIRE', count_key, horizon_s, 'FIELDS', 1, 'b:' .. bucket)
redis.call('HEXPIRE', amt_key, horizon_s, 'FIELDS', 1, 'b:' .. bucket)

local function sum_window(key, minutes)
  local total = 0
  local fields = redis.call('HGETALL', key)
  for i = 1, #fields, 2 do
    local b = tonumber(string.sub(fields[i], 3))
    if b and b > now_bucket - minutes and b <= now_bucket then
      total = total + tonumber(fields[i + 1])
    end
  end
  return total
end

local vector = {
  pan_txn_count_1h = sum_window(count_key, count_win),
  pan_amount_sum_24h = sum_window(amt_key, amt_win),
}
local vector_json = string.format(
  '{"pan_txn_count_1h":%d,"pan_amount_sum_24h":%.6f,"as_of_bucket":%d}',
  vector.pan_txn_count_1h, vector.pan_amount_sum_24h, now_bucket)

redis.call('HSET', dedup_key, event_id, digest .. '|' .. event_id)
redis.call('HSET', vec_key, event_id, vector_json)
redis.call('HEXPIRE', dedup_key, horizon_s, 'FIELDS', 1, event_id)
redis.call('HEXPIRE', vec_key, horizon_s, 'FIELDS', 1, event_id)
return vector_json
