args <- commandArgs(trailingOnly = TRUE)
if (length(args) != 5) stop("expected expression_csv metadata_csv output_csv condition_a condition_b")
if (!requireNamespace("DESeq2", quietly = TRUE)) stop("DESeq2 R package not installed")

expression_csv <- args[[1]]
metadata_csv <- args[[2]]
output_csv <- args[[3]]
condition_a <- args[[4]]
condition_b <- args[[5]]

expression <- read.csv(expression_csv, check.names = FALSE, stringsAsFactors = FALSE)
metadata <- read.csv(metadata_csv, check.names = FALSE, stringsAsFactors = FALSE)
if (!"gene_id" %in% names(expression)) stop("expression matrix needs gene_id")
if (!all(c("sample_id", "condition") %in% names(metadata))) stop("metadata needs sample_id and condition")

sample_ids <- setdiff(names(expression), "gene_id")
if (!all(sample_ids %in% metadata$sample_id)) stop("metadata is missing expression samples")
metadata <- metadata[match(sample_ids, metadata$sample_id), , drop = FALSE]
if (any(is.na(metadata$condition))) stop("metadata contains missing conditions")
if (!all(c(condition_a, condition_b) %in% metadata$condition)) stop("requested conditions are not present")

counts <- expression[sample_ids]
counts[] <- lapply(counts, function(values) {
  numeric_values <- as.numeric(values)
  if (any(!is.finite(numeric_values)) || any(numeric_values < 0)) stop("counts must be finite and non-negative")
  rounded <- round(numeric_values)
  if (any(abs(numeric_values - rounded) > 1e-8)) stop("DESeq2 requires integer counts")
  as.integer(rounded)
})
counts <- as.matrix(counts)
rownames(counts) <- as.character(expression$gene_id)
coldata <- data.frame(condition = factor(metadata$condition, levels = c(condition_a, condition_b)))
rownames(coldata) <- sample_ids

dds <- DESeq2::DESeqDataSetFromMatrix(countData = counts, colData = coldata, design = ~ condition)
dds <- DESeq2::DESeq(dds, quiet = TRUE)
result <- as.data.frame(DESeq2::results(dds, contrast = c("condition", condition_b, condition_a)))
result$gene_id <- rownames(result)
names(result)[names(result) == "baseMean"] <- "base_mean"
names(result)[names(result) == "log2FoldChange"] <- "log2_fc"
names(result)[names(result) == "pvalue"] <- "p_value"
result <- result[c("gene_id", "base_mean", "log2_fc", "p_value", "padj")]
write.csv(result, output_csv, row.names = FALSE)
