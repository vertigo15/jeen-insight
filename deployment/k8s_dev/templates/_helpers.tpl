{{- define "jeen-insights.existingSecretName" -}}
{{- required "global.existingSecret.name is required" .Values.global.existingSecret.name -}}
{{- end }}

{{- define "jeen-insights.externalSecretName" -}}
{{- printf "%s-external" (include "jeen-insights.existingSecretName" .) | trunc 63 | trimSuffix "-" -}}
{{- end }}

{{- define "jeen-insights.migrationName" -}}
{{- $bundleID := required "migration.bundleId is required when migration.enabled=true" .Values.migration.bundleId -}}
{{- if not (regexMatch "^[a-z0-9]([-a-z0-9]*[a-z0-9])?$" $bundleID) -}}
{{- fail "migration.bundleId must be a DNS-1123 label (lowercase alphanumeric characters and hyphens)" -}}
{{- end -}}
{{- $name := printf "%s-migrate-%s" .Release.Name $bundleID -}}
{{- if gt (len $name) 63 -}}
{{- fail (printf "migration Job name %q exceeds the DNS-1123 63-character limit" $name) -}}
{{- end -}}
{{- $name -}}
{{- end }}

{{- define "jeen-insights.migrationImage" -}}
{{- $repository := required "migration.image.repository is required" .Values.migration.image.repository -}}
{{- if .Values.migration.image.digest -}}
{{- printf "%s@%s" $repository .Values.migration.image.digest -}}
{{- else -}}
{{- printf "%s:%s" $repository (required "migration.image.tag is required when migration.image.digest is empty" .Values.migration.image.tag) -}}
{{- end -}}
{{- end }}

{{- define "jeen-insights.migrationSecretName" -}}
{{- default (include "jeen-insights.existingSecretName" .) .Values.migration.existingSecret.name -}}
{{- end }}

{{- define "jeen-insights.migrationSecretOptional" -}}
{{- $optional := .Values.global.existingSecret.optional -}}
{{- if ne .Values.migration.existingSecret.optional nil -}}
{{- $optional = .Values.migration.existingSecret.optional -}}
{{- end -}}
{{- $optional -}}
{{- end }}
