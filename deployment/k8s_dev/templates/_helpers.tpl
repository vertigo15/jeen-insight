{{- define "jeen-insights.existingSecretName" -}}
{{- required "global.existingSecret.name is required" .Values.global.existingSecret.name -}}
{{- end }}

{{- define "jeen-insights.externalSecretName" -}}
{{- printf "%s-external" (include "jeen-insights.existingSecretName" .) | trunc 63 | trimSuffix "-" -}}
{{- end }}
