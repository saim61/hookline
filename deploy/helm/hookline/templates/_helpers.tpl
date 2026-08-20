{{/*
Shared naming and label helpers. Everything derives from one place so a rename cannot leave a
Service selecting nothing - the classic Helm failure, and a silent one: the Deployment is
healthy, the Service has no endpoints, and requests get connection refused.
*/}}

{{- define "hookline.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" }}
{{- end }}

{{- define "hookline.fullname" -}}
{{- if .Values.fullnameOverride }}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- $name := default .Chart.Name .Values.nameOverride }}
{{- if contains $name .Release.Name }}
{{- .Release.Name | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- printf "%s-%s" .Release.Name $name | trunc 63 | trimSuffix "-" }}
{{- end }}
{{- end }}
{{- end }}

{{- define "hookline.chart" -}}
{{- printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/* Labels on every object, including the ones that change between releases. */}}
{{- define "hookline.labels" -}}
helm.sh/chart: {{ include "hookline.chart" . }}
{{ include "hookline.selectorLabels" . }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
app.kubernetes.io/part-of: hookline
{{- end }}

{{/*
Selector labels only. Deliberately excludes version and chart: a Deployment's selector is
immutable after creation, so anything that changes between releases must not appear here or
the next `helm upgrade` fails with a field-is-immutable error.
*/}}
{{- define "hookline.selectorLabels" -}}
app.kubernetes.io/name: {{ include "hookline.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}

{{- define "hookline.image" -}}
{{- printf "%s:%s" .Values.image.repository (.Values.image.tag | default .Chart.AppVersion) }}
{{- end }}

{{- define "hookline.secretName" -}}
{{- .Values.secrets.existingSecret | default (printf "%s-secrets" (include "hookline.fullname" .)) }}
{{- end }}

{{/*
Credentials, as env entries. Kept in one place so a setting cannot be applied to the API and
forgotten on the worker - which is how two processes end up disagreeing about
max_delivery_attempts.

Split from envFrom because they are different keys in a container spec; a single helper
emitting both would produce invalid YAML wherever it was used.
*/}}
{{- define "hookline.secretEnv" -}}
- name: HOOKLINE_DATABASE_URL
  valueFrom:
    secretKeyRef:
      name: {{ include "hookline.secretName" . }}
      key: database-url
- name: HOOKLINE_REDIS_URL
  valueFrom:
    secretKeyRef:
      name: {{ include "hookline.secretName" . }}
      key: redis-url
{{- end }}

{{/* Everything non-secret, from the ConfigMap. */}}
{{- define "hookline.envFrom" -}}
- configMapRef:
    name: {{ include "hookline.fullname" . }}-config
{{- end }}

{{/*
readOnlyRootFilesystem is on, so anything that writes to disk needs an explicit emptyDir.
Python wants a writable temp dir; nothing else here does.
*/}}
{{- define "hookline.tmpVolume" -}}
- name: tmp
  emptyDir: {}
{{- end }}

{{- define "hookline.tmpVolumeMount" -}}
- name: tmp
  mountPath: /tmp
{{- end }}
