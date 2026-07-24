{{- define "jeen-insights-ui.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" }}
{{- end }}

{{- define "jeen-insights-ui.fullname" -}}
{{- if .Values.fullnameOverride }}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- printf "%s-%s" .Release.Name (include "jeen-insights-ui.name" .) | trunc 63 | trimSuffix "-" }}
{{- end }}
{{- end }}

{{- define "jeen-insights-ui.labels" -}}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | quote }}
app.kubernetes.io/name: {{ include "jeen-insights-ui.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{ with .Values.global.commonLabels }}
{{- toYaml . }}
{{- end }}
{{- end }}

{{- define "jeen-insights-ui.selectorLabels" -}}
app.kubernetes.io/name: {{ include "jeen-insights-ui.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}

{{- define "jeen-insights-ui.serviceAccountName" -}}
{{- if .Values.serviceAccount.create }}
{{- include "jeen-insights-ui.fullname" . }}
{{- else }}
{{- default "default" .Values.serviceAccount.name }}
{{- end }}
{{- end }}
