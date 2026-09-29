{{- define "rtdp.name" -}}{{ .Release.Name }}{{- end -}}
{{- define "rtdp.saName" -}}{{ .Values.serviceAccount.name | default .Release.Name }}{{- end -}}
{{- define "rtdp.labels" -}}
app.kubernetes.io/name: {{ include "rtdp.name" . }}
app.kubernetes.io/part-of: rtdp
app.kubernetes.io/managed-by: argocd
{{- end -}}
