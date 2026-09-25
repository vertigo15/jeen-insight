{{- define "jeen-insights-api.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" }}
{{- end }}

{{- define "jeen-insights-api.fullname" -}}
{{- if .Values.fullnameOverride }}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- printf "%s-%s" .Release.Name (include "jeen-insights-api.name" .) | trunc 63 | trimSuffix "-" }}
{{- end }}
{{- end }}

{{- define "jeen-insights-api.labels" -}}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | quote }}
app.kubernetes.io/name: {{ include "jeen-insights-api.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{ with .Values.global.commonLabels }}
{{- toYaml . }}
{{- end }}
{{- end }}

{{- define "jeen-insights-api.selectorLabels" -}}
app.kubernetes.io/name: {{ include "jeen-insights-api.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}

{{- define "jeen-insights-api.serviceAccountName" -}}
{{- if .Values.serviceAccount.create }}
{{- include "jeen-insights-api.fullname" . }}
{{- else }}
{{- default "default" .Values.serviceAccount.name }}
{{- end }}
{{- end }}

{{- define "jeen-insights-api.image" -}}
{{- $repository := required "jeen-insights-api.image.repository is required" .Values.image.repository -}}
{{- if .Values.image.digest -}}
{{- printf "%s@%s" $repository .Values.image.digest -}}
{{- else -}}
{{- printf "%s:%s" $repository (required "jeen-insights-api.image.tag is required when image.digest is empty" .Values.image.tag) -}}
{{- end -}}
{{- end }}

{{- define "jeen-insights-api.existingSecretName" -}}
{{- default (required "global.existingSecret.name is required" .Values.global.existingSecret.name) .Values.existingSecret.name -}}
{{- end }}

{{- define "jeen-insights-api.existingSecretOptional" -}}
{{- $optional := .Values.global.existingSecret.optional -}}
{{- if ne .Values.existingSecret.optional nil -}}
{{- $optional = .Values.existingSecret.optional -}}
{{- end -}}
{{- $optional -}}
{{- end }}
