-- Kiwop Labs · «Agentes de IA en producción»: agregado trimestral de la telemetría de
-- Nexo (nexo.kiwop.com, Postgres). SOLO agregados y distribuciones: ni nombres de
-- clientes, ni textos, ni ids. Se ejecuta en el servidor de Nexo con psql (solo
-- lectura) desde scripts/labs/agentes-produccion/medir.sh y devuelve UNA fila JSON.
--
-- Parámetros (psql -v): dias (ventana en días, 90) y hasta (fecha fin, 'YYYY-MM-DD').
-- Todo se mide sobre [hasta - dias, hasta).
--
-- Fuentes (ver docs/mapa-sistema/07-ia-brain.md en nexo-platform):
--   proactive_agent_executions  una fila por ejecución de agente proactivo
--   task_comments (source='api') comentarios escritos por un agente; validated_at = firmado
--   tasks                       worker autónomo (agent_claimed_at, pr_url), director (agent_triaged_at)
--   agent_activity_pings        pasadas del worker (metadata.kind='auto_run', queue)
--   triage_items                clasificación de correo entrante
--   guardian_reviews            revisión del correo saliente
--   ask_josep_messages          consultas al «cerebro» (escalated, helpful)
--   lead_agent_sequences        agente de seguimiento de leads
--   ai_usage_events             metering por llamada (feature, tokens, coste; meta.auth_mode)
--   ai_cli_sessions             sesiones del CLI en el servidor (origin, tokens, coste lista USD)
--   scheduled_task_runs         historial de todos los crons
--
-- Gotchas conocidos: los eventos anteriores a ago-2026 cobraban la caché a tarifa
-- plena; lo que va por console_auth (suscripción) no se paga por token (aquí sale
-- como «equivalente API», separado); agent_token_usage inflaba x3 y NO se usa.

WITH v AS (
  SELECT (:'hasta')::date AS hasta, ((:'hasta')::date - (:'dias')::int) AS desde
),
ej AS (
  SELECT e.*
  FROM proactive_agent_executions e, v
  WHERE e.started_at >= v.desde AND e.started_at < v.hasta
),
agentes AS (
  SELECT json_build_object(
    'ejecuciones', count(*),
    'completadas', count(*) FILTER (WHERE status = 'completed'),
    'fallidas', count(*) FILTER (WHERE status = 'failed'),
    'exito_pct', round(100.0 * count(*) FILTER (WHERE status = 'completed') / NULLIF(count(*), 0), 1),
    'agentes_activos', count(DISTINCT agent_id),
    'duracion_mediana_s', round((percentile_cont(0.5) WITHIN GROUP (ORDER BY EXTRACT(EPOCH FROM (completed_at - started_at))) FILTER (WHERE completed_at IS NOT NULL))::numeric, 1),
    'duracion_p90_s', round((percentile_cont(0.9) WITHIN GROUP (ORDER BY EXTRACT(EPOCH FROM (completed_at - started_at))) FILTER (WHERE completed_at IS NOT NULL))::numeric, 1),
    'por_nivel_alerta', (SELECT json_object_agg(COALESCE(alert_level, 'sin nivel'), n) FROM (SELECT alert_level, count(*) n FROM ej GROUP BY alert_level) x)
  ) AS j FROM ej
),
com AS (
  SELECT c.*
  FROM task_comments c, v
  WHERE c.source = 'api' AND c.created_at >= v.desde AND c.created_at < v.hasta
),
comentarios AS (
  SELECT json_build_object(
    'propuestos', count(*),
    'notas_internas', count(*) FILTER (WHERE is_internal_note),
    'para_cliente', count(*) FILTER (WHERE NOT is_internal_note),
    'firmados', count(*) FILTER (WHERE validated_at IS NOT NULL),
    'firmados_pct', round(100.0 * count(*) FILTER (WHERE validated_at IS NOT NULL) / NULLIF(count(*), 0), 1),
    'para_cliente_firmados', count(*) FILTER (WHERE NOT is_internal_note AND validated_at IS NOT NULL),
    'para_cliente_firmados_pct', round(100.0 * count(*) FILTER (WHERE NOT is_internal_note AND validated_at IS NOT NULL) / NULLIF(count(*) FILTER (WHERE NOT is_internal_note), 0), 1),
    'horas_hasta_firma_mediana', round((percentile_cont(0.5) WITHIN GROUP (ORDER BY EXTRACT(EPOCH FROM (validated_at - created_at)) / 3600) FILTER (WHERE validated_at IS NOT NULL AND NOT is_internal_note AND validated_at > created_at + interval '1 minute'))::numeric, 1)
  ) AS j FROM com
),
worker AS (
  SELECT json_build_object(
    'tareas_reclamadas', (SELECT count(*) FROM tasks t, v WHERE t.agent_claimed_at >= v.desde AND t.agent_claimed_at < v.hasta),
    'pr_entregados', (SELECT count(*) FROM tasks t, v WHERE t.pr_url IS NOT NULL AND t.agent_claimed_at >= v.desde AND t.agent_claimed_at < v.hasta),
    'horas_reclamo_a_pr_mediana', (SELECT round((percentile_cont(0.5) WITHIN GROUP (ORDER BY EXTRACT(EPOCH FROM (t.updated_at - t.agent_claimed_at)) / 3600))::numeric, 1) FROM tasks t, v WHERE t.pr_url IS NOT NULL AND t.agent_claimed_at >= v.desde AND t.agent_claimed_at < v.hasta),
    'pasadas', (SELECT count(*) FROM agent_activity_pings p, v WHERE p.metadata->>'kind' = 'auto_run' AND p.at >= v.desde AND p.at < v.hasta),
    'cola_media', (SELECT round(avg((p.metadata->>'queue')::numeric), 2) FROM agent_activity_pings p, v WHERE p.metadata->>'kind' = 'auto_run' AND p.at >= v.desde AND p.at < v.hasta AND (p.metadata->>'queue') IS NOT NULL),
    'tareas_triadas', (SELECT count(*) FROM tasks t, v WHERE t.agent_triaged_at >= v.desde AND t.agent_triaged_at < v.hasta),
    'tareas_delegadas', (SELECT count(*) FROM tasks t, v WHERE t.agent_triaged_at >= v.desde AND t.agent_triaged_at < v.hasta AND t.agent_autonomy = 'auto'),
    'fallbacks', (SELECT count(*) FROM tasks t, v WHERE t.agent_fallback_at >= v.desde AND t.agent_fallback_at < v.hasta)
  ) AS j
),
tr AS (
  SELECT t.* FROM triage_items t, v WHERE t.created_at >= v.desde AND t.created_at < v.hasta
),
triage AS (
  SELECT json_build_object(
    'items', count(*),
    'por_tipo', (SELECT json_object_agg(type, n) FROM (SELECT type, count(*) n FROM tr GROUP BY type) x),
    'por_confianza', (SELECT json_object_agg(COALESCE(confidence, 'sin dato'), n) FROM (SELECT confidence, count(*) n FROM tr GROUP BY confidence) x),
    'clasificador_fallido_pct', round(100.0 * count(*) FILTER (WHERE classifier_failed) / NULLIF(count(*), 0), 1),
    'latencia_mediana_ms', round((percentile_cont(0.5) WITHIN GROUP (ORDER BY classifier_latency_ms))::numeric, 0),
    'decididos', count(*) FILTER (WHERE decided_at IS NOT NULL),
    'por_decision', (SELECT json_object_agg(COALESCE(decision, 'sin decidir'), n) FROM (SELECT decision, count(*) n FROM tr GROUP BY decision) x),
    'tareas_creadas', count(*) FILTER (WHERE created_task_id IS NOT NULL)
  ) AS j FROM tr
),
gr AS (
  SELECT g.* FROM guardian_reviews g, v WHERE g.created_at >= v.desde AND g.created_at < v.hasta
),
guardian AS (
  SELECT json_build_object(
    'revisiones', count(*),
    'por_veredicto', (SELECT json_object_agg(verdict, n) FROM (SELECT verdict, count(*) n FROM gr GROUP BY verdict) x),
    'aceptadas_pct', round(100.0 * count(*) FILTER (WHERE accepted) / NULLIF(count(*) FILTER (WHERE accepted IS NOT NULL), 0), 1),
    'latencia_mediana_ms', round((percentile_cont(0.5) WITHIN GROUP (ORDER BY latency_ms))::numeric, 0),
    'sugerencias_media', round(avg(suggestions_count)::numeric, 2)
  ) AS j FROM gr
),
aj AS (
  SELECT a.* FROM ask_josep_messages a, v WHERE a.created_at >= v.desde AND a.created_at < v.hasta
),
askjosep AS (
  SELECT json_build_object(
    'consultas', count(*),
    'escaladas', count(*) FILTER (WHERE escalated),
    'escaladas_pct', round(100.0 * count(*) FILTER (WHERE escalated) / NULLIF(count(*), 0), 1),
    'valoradas', count(*) FILTER (WHERE helpful IS NOT NULL),
    'utiles_pct', round(100.0 * count(*) FILTER (WHERE helpful = 1) / NULLIF(count(*) FILTER (WHERE helpful IS NOT NULL), 0), 1),
    'similitud_mediana', round((percentile_cont(0.5) WITHIN GROUP (ORDER BY top_similarity))::numeric, 3)
  ) AS j FROM aj
),
leads AS (
  SELECT json_build_object(
    'secuencias', count(*),
    'por_estado', (SELECT json_object_agg(status, n) FROM (SELECT status, count(*) n FROM lead_agent_sequences GROUP BY status) x),
    'seguimientos_enviados', COALESCE(sum(followups_sent), 0),
    'por_motivo_parada', (SELECT json_object_agg(COALESCE(stop_reason, 'activa'), n) FROM (SELECT stop_reason, count(*) n FROM lead_agent_sequences GROUP BY stop_reason) x)
  ) AS j FROM lead_agent_sequences
),
ue AS (
  SELECT e.*, COALESCE(e.meta->>'auth_mode', '') AS auth_mode, COALESCE(NULLIF(e.meta->>'source', ''), '(sin registrar)') AS src
  FROM ai_usage_events e, v WHERE e.created_at >= v.desde AND e.created_at < v.hasta
),
coste_api AS (
  SELECT json_build_object(
    'llamadas', count(*),
    'tokens_entrada', COALESCE(sum(input_tokens), 0),
    'tokens_salida', COALESCE(sum(output_tokens), 0),
    'coste_facturado_eur', round(COALESCE(sum(cost_cents) FILTER (WHERE auth_mode <> 'console_auth'), 0) / 100.0, 2),
    'coste_equivalente_suscripcion_eur', round(COALESCE(sum(cost_cents) FILTER (WHERE auth_mode = 'console_auth'), 0) / 100.0, 2),
    'por_feature', (SELECT json_object_agg(feature, json_build_object('llamadas', n, 'tokens_entrada', ti, 'tokens_salida', tso, 'coste_eur', c)) FROM (
        SELECT feature, count(*) n, sum(input_tokens) ti, sum(output_tokens) tso, round(sum(cost_cents) / 100.0, 2) c FROM ue GROUP BY feature ORDER BY c DESC) x),
    'por_proveedor', (SELECT json_object_agg(provider, n) FROM (SELECT provider, count(*) n FROM ue GROUP BY provider) x),
    'modelos_distintos', count(DISTINCT model),
    'latencia_mediana_ms', round((percentile_cont(0.5) WITHIN GROUP (ORDER BY (meta->>'latency_ms')::numeric) FILTER (WHERE (meta->>'latency_ms') IS NOT NULL))::numeric, 0),
    'cache_lectura_pct', round(100.0 * count(*) FILTER (WHERE meta->>'cache' = 'read') / NULLIF(count(*), 0), 1)
  ) AS j FROM ue
),
cs AS (
  SELECT s.* FROM ai_cli_sessions s, v WHERE s.started_on >= v.desde AND s.started_on < v.hasta
),
coste_cli AS (
  SELECT json_build_object(
    'sesiones', count(*),
    'turnos', COALESCE(sum(turns), 0),
    'tokens_entrada', COALESCE(sum(input_tokens), 0),
    'tokens_salida', COALESCE(sum(output_tokens), 0),
    'tokens_cache_lectura', COALESCE(sum(cache_read_tokens), 0),
    'tokens_cache_creacion', COALESCE(sum(cache_creation_tokens), 0),
    'coste_lista_usd', round(COALESCE(sum(cost_cents), 0) / 100.0, 2),
    'por_origen', (SELECT json_object_agg(origin, json_build_object('sesiones', n, 'turnos', t, 'coste_lista_usd', c)) FROM (
        SELECT origin, count(*) n, sum(turns) t, round(sum(cost_cents) / 100.0, 2) c FROM cs GROUP BY origin ORDER BY c DESC) x)
  ) AS j FROM cs
),
sr AS (
  SELECT r.* FROM scheduled_task_runs r, v WHERE r.started_at >= v.desde AND r.started_at < v.hasta
),
crons AS (
  SELECT json_build_object(
    'ejecuciones', count(*),
    'tareas_distintas', count(DISTINCT task),
    'por_resultado', (SELECT json_object_agg(outcome, n) FROM (SELECT outcome, count(*) n FROM sr GROUP BY outcome) x),
    'fallidas_pct', round(100.0 * count(*) FILTER (WHERE outcome = 'failed') / NULLIF(count(*), 0), 2),
    'duracion_mediana_ms', round((percentile_cont(0.5) WITHIN GROUP (ORDER BY duration_ms))::numeric, 0)
  ) AS j FROM sr
)
SELECT json_build_object(
  'ventana', json_build_object('desde', (SELECT desde FROM v), 'hasta', (SELECT hasta FROM v), 'dias', (:'dias')::int),
  'generado_en', now(),
  'agentes_proactivos', (SELECT j FROM agentes),
  'comentarios_agente', (SELECT j FROM comentarios),
  'worker_autonomo', (SELECT j FROM worker),
  'triage_correo', (SELECT j FROM triage),
  'guardian_correo', (SELECT j FROM guardian),
  'ask_josep', (SELECT j FROM askjosep),
  'agente_leads', (SELECT j FROM leads),
  'coste_api', (SELECT j FROM coste_api),
  'coste_cli', (SELECT j FROM coste_cli),
  'crons', (SELECT j FROM crons)
);
