{{- define "gdash.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "gdash.fullname" -}}
{{- if .Values.fullnameOverride -}}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- $name := default .Chart.Name .Values.nameOverride -}}
{{- if contains $name .Release.Name -}}
{{- .Release.Name | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- printf "%s-%s" .Release.Name $name | trunc 63 | trimSuffix "-" -}}
{{- end -}}
{{- end -}}
{{- end -}}

{{- define "gdash.namespace" -}}
{{- default .Release.Namespace .Values.namespaceOverride -}}
{{- end -}}

{{- define "gdash.labels" -}}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" }}
app.kubernetes.io/name: {{ include "gdash.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- with .Values.commonLabels }}
{{ toYaml . }}
{{- end }}
{{- end -}}

{{/*
Render one dashboard ConfigMap as YAML.
Args (dict): root, folderKey, name, file, content
The caller decides enablement and emits the `---` separator. Building a dict and
piping through toYaml avoids block-scalar/indentation pitfalls with embedded JSON.
*/}}
{{- define "gdash.configmap" -}}
{{- $root := .root -}}
{{- $folderKey := .folderKey -}}
{{- $fcfg := (index $root.Values.folders $folderKey) | default dict -}}
{{- $folderName := $fcfg.folder | default $folderKey -}}
{{- $dsUid := $fcfg.datasourceUid | default $root.Values.datasource.uid -}}
{{- $content := .content -}}
{{- if $dsUid }}{{- $content = $content | replace "${datasource}" $dsUid -}}{{- end -}}
{{- $cmName := printf "%s-%s-%s" (include "gdash.fullname" $root) $folderKey .name | lower | replace " " "-" | replace "_" "-" | trunc 63 | trimSuffix "-" -}}
{{- $labels := merge (dict
      "helm.sh/chart" (printf "%s-%s" $root.Chart.Name $root.Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-")
      "app.kubernetes.io/name" (include "gdash.name" $root)
      "app.kubernetes.io/instance" $root.Release.Name
      "app.kubernetes.io/managed-by" $root.Release.Service
      $root.Values.sidecar.label ($root.Values.sidecar.labelValue | toString)
    ) $root.Values.commonLabels -}}
{{- $annotations := merge (dict $root.Values.folderAnnotation $folderName) $root.Values.commonAnnotations -}}
{{- $cm := dict
      "apiVersion" "v1"
      "kind" "ConfigMap"
      "metadata" (dict "name" $cmName "namespace" (include "gdash.namespace" $root) "labels" $labels "annotations" $annotations)
      "data" (dict .file $content) -}}
{{ $cm | toYaml }}
{{- end -}}

{{/*
Decide if a (folderKey, name) dashboard is enabled. Returns "true" or "".
Uses hasKey so an explicit enabled:false is honoured (sprig `default` would not).
*/}}
{{- define "gdash.enabled" -}}
{{- $root := .root -}}
{{- $fcfg := (index $root.Values.folders .folderKey) | default dict -}}
{{- $folderEnabled := true -}}
{{- if hasKey $fcfg "enabled" }}{{- $folderEnabled = $fcfg.enabled -}}{{- end -}}
{{- $dcfg := dict -}}
{{- if $fcfg.dashboards }}{{- $dcfg = (index $fcfg.dashboards .name) | default dict -}}{{- end -}}
{{- $dashEnabled := true -}}
{{- if hasKey $dcfg "enabled" }}{{- $dashEnabled = $dcfg.enabled -}}{{- end -}}
{{- if and $folderEnabled $dashEnabled }}true{{- end -}}
{{- end -}}
